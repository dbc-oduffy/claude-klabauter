"""Bounded machine-local reads: run_bounded, dump, get.

Every `_machine_local.py` spawn a claude-klabauter-owned caller makes goes through here: a hard kill
ceiling that reaps the child and never retries, an in-process read where the impl is a `.py`,
a per-process mtime-keyed cache, and a box-wide single-flight on dump so concurrent callers
consume one in-flight dump instead of starting another.

Module-top imports stay stdlib-only; cold CLI callers import this. `locked_write` and the
process-kill primitives load lazily on the paths that need them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# The single kill ceiling. Not a budget: the budget is the 500ms brightline (DR-344). No
# caller passes a larger value; run_bounded clamps one that does.
ML_SPAWN_TIMEOUT_S = 2.0

# How long a cached answer, per-process or shared, outlives its mtime key. Autodiscovery
# results move without any registry file changing, so the key alone cannot bound staleness;
# a failed read is remembered for the same window so callers do not re-spawn into a stall.
CACHE_TTL_S = 30.0

_REAP_WAIT_S = 2.0

# Waiter ceilings (process lock, box lock): a caller behind an in-flight dump gives up well
# inside the brightline rather than stacking a second kill ceiling onto it.
ML_WAIT_TIMEOUT_S = 0.5
_DEFAULT_ARGS = ("--include-unset",)
_MISS = object()

_CACHE: dict = {}
_DUMP_MEMO: dict = {}
_PROCESS_FLIGHT = threading.Lock()


def _argv(impl: str, *tail: str) -> list[str]:
    """Interpreter plus script for a `.py` impl; the impl itself for an executable test stub."""
    if impl.endswith(".py") or impl.endswith(".py3"):
        return [sys.executable, impl, *tail]
    return [impl, *tail]


def _descendants(rows, root_pid: int) -> list:
    """`root_pid` and every process below it in a ProcRow snapshot."""
    by_parent: dict = {}
    by_pid: dict = {}
    for r in rows:
        by_parent.setdefault(r.ppid, []).append(r)
        by_pid[r.pid] = r
    tree = []
    seen = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if pid in by_pid:
            tree.append(by_pid[pid])
        stack.extend(c.pid for c in by_parent.get(pid, ()))
    return tree


def _kill_tree_windows(proc: subprocess.Popen, snapshot=None, kill=None) -> None:
    """Verified kill of proc's whole tree; falls back to proc.kill() when the table is unreadable."""
    try:
        if snapshot is None or kill is None:
            from coordinator_core.bash_guards import _host_probe, _process_kill

            snapshot = snapshot or _host_probe.snapshot
            kill = kill or _process_kill
        from coordinator_core.bash_guards._taskstop_kill import kill_tree_verified

        rows = snapshot()
        tree = _descendants(rows, proc.pid) if rows else []
        if tree:
            kill_tree_verified(tree, snapshot, kill)
    except Exception:
        pass
    with contextlib.suppress(OSError):
        proc.kill()


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        _kill_tree_windows(proc)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        with contextlib.suppress(OSError):
            proc.kill()


_DEFAULT_MAX_INFLIGHT = 16
_SLOT_SWEEP_PAUSE_S = 0.02
_CONTENDED = object()
_WIN_JOB = None


def _slot_dir() -> Path:
    """Box-wide in-flight slot directory, shared with the `_machine_local.py` kernel."""
    override = os.environ.get("MACHINE_LOCAL_SLOT_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "coordinator" / "machine-local-slots"


def _slot_count() -> int:
    try:
        n = int(os.environ.get("MACHINE_LOCAL_MAX_INFLIGHT", ""))
    except ValueError:
        return _DEFAULT_MAX_INFLIGHT
    return n if n >= 1 else _DEFAULT_MAX_INFLIGHT


def _probe_order(n: int) -> list[int]:
    """Slot indices to try, starting at pid % n so concurrent callers fan out."""
    start = os.getpid() % n
    return [(start + k) % n for k in range(n)]


def _claim_slot():
    """(state, fd): "held" with the locked fd, "contended" (all N held), or "unusable" (dir/open failed)."""
    from coordinator_core import locked_write

    n = _slot_count()
    directory = _slot_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "unusable", None
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    deadline = time.monotonic() + ML_WAIT_TIMEOUT_S
    while True:
        for i in _probe_order(n):
            try:
                fd = os.open(str(directory / f"slot-{i}.lock"), flags, 0o666)
            except OSError:
                return "unusable", None
            try:
                if locked_write._plat_try_lock(fd):
                    return "held", fd
            except OSError:
                os.close(fd)
                return "unusable", None
            os.close(fd)
        if time.monotonic() >= deadline:
            return "contended", None
        time.sleep(_SLOT_SWEEP_PAUSE_S)


@contextlib.contextmanager
def _box_slot():
    """Hold one of the box-wide in-flight slots for the spawn's span; the OS drops it on death.

    Yields "held", "contended" or "unusable". An unusable slot dir fails open: the caller
    spawns without a slot.
    """
    state, fd = _claim_slot()
    try:
        yield state
    finally:
        if fd is not None:
            from coordinator_core import locked_write

            with contextlib.suppress(OSError):
                locked_write._plat_unlock(fd)
            with contextlib.suppress(OSError):
                os.close(fd)


def _win_job_api():
    """ctypes bindings for a kill-on-close Job Object; cached. Windows only."""
    global _WIN_JOB
    if _WIN_JOB is not None:
        return _WIN_JOB
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", Basic),
            ("IoInfo", ctypes.c_ulonglong * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

    def create():
        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            k32.CloseHandle(job)
            raise ctypes.WinError(err)
        return job

    def assign(job, proc) -> bool:
        return bool(k32.AssignProcessToJobObject(job, int(proc._handle)))

    def resume(proc) -> None:
        if ntdll.NtResumeProcess(int(proc._handle)) != 0:
            raise OSError("NtResumeProcess failed")

    _WIN_JOB = (create, assign, resume, k32.CloseHandle)
    return _WIN_JOB


def _close_job(job) -> None:
    if job is not None:
        with contextlib.suppress(Exception):
            _win_job_api()[3](job)


def _set_pdeathsig_preexec():
    """preexec_fn that asks the kernel to SIGKILL the child when its parent thread dies, or None.

    Linux only. PDEATHSIG tracks the spawning THREAD, so the caller must reap on that thread.
    libc is bound before fork; the child body is the prctl call and a parent-gone check only.
    preexec_fn is not fork-safe under threads in general; here the body is bounded to those
    two pre-bound calls, so the deadlock surface is minimal and accepted.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes

        prctl = ctypes.CDLL(None).prctl
    except Exception:
        return None
    parent = os.getpid()
    kill = int(signal.SIGKILL)
    getppid = os.getppid
    exit_ = os._exit

    def _preexec() -> None:
        prctl(1, kill, 0, 0, 0)  # PR_SET_PDEATHSIG
        if getppid() != parent:
            exit_(1)

    return _preexec


def _popen(argv, env=None):
    """(proc, job): the child started under kill-on-close where the OS offers it, else bare.

    Windows: suspended, assigned to a kill-on-close Job Object, then resumed, so the child
    cannot run or fork outside the job. A failed job falls back to the plain path.
    """
    kwargs: dict = {}
    job = None
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
        try:
            job = _win_job_api()[0]()
            flags |= 0x00000004  # CREATE_SUSPENDED
        except Exception:
            job = None
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
        preexec = _set_pdeathsig_preexec()
        if preexec is not None:
            kwargs["preexec_fn"] = preexec
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
    except BaseException:
        _close_job(job)
        raise
    if job is not None:
        try:
            if not _win_job_api()[1](job, proc):
                _close_job(job)
                job = None
            _win_job_api()[2](proc)
        except BaseException:
            _reap(proc)
            _close_job(job)
            raise
    return proc, job


def _run(argv, timeout: float, env=None):
    """CompletedProcess, None (unstartable or killed at the ceiling) or _CONTENDED (no slot)."""
    with _box_slot() as slot:
        if slot == "contended":
            return _CONTENDED
        try:
            proc, job = _popen(argv, env)
        except (OSError, ValueError):
            return None
        try:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _reap(proc)
                return None
            except BaseException:
                _reap(proc)
                raise
            return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)
        finally:
            _close_job(job)


def run_bounded(argv, *, timeout: float = ML_SPAWN_TIMEOUT_S, env=None):
    """Run argv once under a hard kill ceiling, a box-wide in-flight cap and kill-on-close.

    Returns the CompletedProcess, or None when the child could not start, outlived the
    ceiling, or no box-wide slot freed within ML_WAIT_TIMEOUT_S (nothing is spawned then).
    On expiry the whole process tree is killed and the direct child reaped before return;
    the child also dies with its caller (Job Object on Windows, parent-death signal on
    Linux; macOS has neither). Never retries: None means the caller takes its existing
    fallback, never a second spawn of the same read. `timeout` is clamped to
    ML_SPAWN_TIMEOUT_S. `env` is the child's full environment (None inherits); stdin is
    always closed.
    """
    cp = _run(argv, min(float(timeout), ML_SPAWN_TIMEOUT_S), env)
    return None if cp is _CONTENDED else cp


def _reap(proc: subprocess.Popen) -> None:
    _kill_tree(proc)
    try:
        proc.wait(timeout=_REAP_WAIT_S)
    except subprocess.TimeoutExpired:
        pass
    for stream in (proc.stdout, proc.stderr):
        with contextlib.suppress(Exception):
            if stream is not None:
                stream.close()


def _registry_dir() -> str:
    """Where `_machine_local.py` reads its registry: the same precedence ladder it uses."""
    override = os.environ.get("MACHINE_LOCAL_REGISTRY_DIR")
    if override:
        return override
    return os.path.join(_settings_root(), "machine-local")


def _settings_root() -> str:
    root = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if not root:
        home = os.environ.get("CLAUDE_HOME") or str(Path.home())
        root = os.path.join(home, ".coordinator-claude-settings")
    return root


def kernel_path() -> str:
    """The `_machine_local.py` kernel under the settings home, same ladder as `_registry_dir`."""
    return os.path.join(_settings_root(), "bin", "_machine_local.py")


def _mtime(path: str):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _cache_key(impl: str, args: tuple) -> tuple:
    reg = _registry_dir()
    return (
        impl,
        args,
        reg,
        _mtime(os.path.join(reg, "registry.toml")),
        _mtime(os.path.join(reg, "registry.local.toml")),
        _mtime(impl),
    )


def _cache_get(key: tuple):
    entry = _CACHE.get(key)
    if entry is None:
        return _MISS
    payload, at = entry
    if time.monotonic() - at > CACHE_TTL_S:
        _CACHE.pop(key, None)
        return _MISS
    return payload


def _cache_put(key: tuple, payload) -> None:
    _CACHE[key] = (payload, time.monotonic())


def _parse_dump_args(args: tuple):
    """(include_unset, prefix) when args are only flags the in-process path honours, else None."""
    include_unset = False
    prefix = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--include-unset":
            include_unset = True
        elif a == "--prefix" and i + 1 < len(args):
            prefix = args[i + 1]
            i += 1
        elif a.startswith("--prefix="):
            prefix = a[len("--prefix="):]
        elif a == "--format=json":
            pass
        elif a == "--format" and i + 1 < len(args) and args[i + 1] == "json":
            i += 1
        else:
            return None
        i += 1
    return include_unset, prefix


def _load_kernel(impl: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location("_machine_local_kernel", impl)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _stat_sig(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _layer_signature(impl: str, args: tuple) -> tuple | None:
    """Everything the in-process dump reads: stat of the kernel and of every `*.toml` in the
    registry dir (base, local and each concern layer), plus the env the resolver consults.
    None when the registry dir cannot be listed, so the caller recomputes rather than trusts."""
    reg = _registry_dir()
    try:
        with os.scandir(reg) as it:
            layers = sorted(
                (e.name, _stat_sig(e.path)) for e in it if e.name.endswith(".toml")
            )
    except FileNotFoundError:
        layers = []
    except OSError:
        return None
    env = tuple(
        sorted(
            (k, v)
            for k, v in os.environ.items()
            if k.startswith(("REPO_", "MACHINE_LOCAL_")) or k == "COORDINATOR_SETTINGS_HOME"
        )
    )
    return (impl, args, reg, _stat_sig(impl), tuple(layers), env)


def _dump_in_process(impl: str, args: tuple):
    """(payload,) when the kernel answered in-process, else None (caller falls through).

    A successful dump is memoized per process under `_layer_signature`, so N reads over an
    unchanged registry cost one dump; a failed one is never memoized.
    """
    sig = _layer_signature(impl, args)
    if sig is not None:
        hit = _DUMP_MEMO.get((impl, args))
        if hit is not None and hit[0] == sig:
            return (dict(hit[1]),)
    answered = _compute_dump_in_process(impl, args)
    if sig is not None and answered is not None and answered[0] is not None:
        _DUMP_MEMO[(impl, args)] = (sig, answered[0])
        return (dict(answered[0]),)
    return answered


def _compute_dump_in_process(impl: str, args: tuple):
    """(payload,) when the kernel answered in-process, else None (caller falls through).

    payload is None when a key hit an operational failure, the same answer a `dump` exit
    code of 2 gives a spawning caller. The kernel's `cmd_dump`/`main` are never called: they
    print and may `sys.exit`.
    """
    parsed = _parse_dump_args(args)
    if parsed is None:
        return None
    include_unset, prefix = parsed
    mod = None
    try:
        mod = _load_kernel(impl)
        mod._toml_memo = {}
        reg_dir = mod._registry_dir()
        layers = mod._build_resolution_layers(reg_dir)
        keys = mod._all_keys(layers)
        if prefix:
            keys = [k for k in keys if k == prefix or k.startswith(f"{prefix}.")]
        values: dict = {}
        for k in keys:
            rc, val = mod.resolve_one(k, layers)
            if rc == mod.EXIT_OK and val is not None:
                values[k] = val
            elif rc == mod.EXIT_OPERATIONAL:
                return (None,)
            elif include_unset:
                values[k] = None
        return (values,)
    except (SystemExit, Exception):
        return None
    finally:
        if mod is not None:
            with contextlib.suppress(Exception):
                mod._toml_memo = None


def _spawn_dump(impl: str, args: tuple):
    """The spawned dump's dict, None on failure, or _CONTENDED when no box slot freed."""
    cp = _run(_argv(impl, "dump", *args), ML_SPAWN_TIMEOUT_S)
    if cp is _CONTENDED:
        return _CONTENDED
    if cp is None or cp.returncode != 0:
        return None
    try:
        parsed = json.loads(cp.stdout or "{}")
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _shared_cache_path(impl: str, args: tuple):
    try:
        from coordinator_core.environment import _settings_home

        home = _settings_home(os.environ)
    except Exception:
        return None
    if home is None:
        return None
    digest = hashlib.sha1(repr((impl, args)).encode("utf-8")).hexdigest()[:16]
    return Path(home) / "machine-local-dump-cache" / f"{digest}.json"


def _key_fingerprint(key: tuple) -> list:
    return [str(part) for part in key[2:]]


def _shared_read(path, key: tuple):
    if path is None:
        return _MISS
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(rec["at"])
        if rec["key"] == _key_fingerprint(key) and -5.0 <= age <= CACHE_TTL_S:
            payload = rec["payload"]
            if payload is None or isinstance(payload, dict):
                return payload
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return _MISS


def _shared_write(path, key: tuple, payload) -> None:
    if path is None:
        return
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps({"key": _key_fingerprint(key), "at": time.time(), "payload": payload}),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _box_wide_flight(impl: str, args: tuple, key: tuple):
    """One dump per registry state across every process on the box; waiters read its result.

    A fresh shared-cache entry is served before the box lock is touched. Returns _CONTENDED
    when no in-flight slot freed: nothing was spawned and nothing is cached.
    """
    path = _shared_cache_path(impl, args)
    shared = _shared_read(path, key)
    if shared is not _MISS:
        return shared
    try:
        from coordinator_core import locked_write
    except Exception:
        return _spawn_dump(impl, args)
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(
            locked_write.held_lock(Path(os.path.abspath(key[2])), timeout=ML_SPAWN_TIMEOUT_S)
        )
    except locked_write.LockTimeout:
        return None
    except Exception:
        return _spawn_dump(impl, args)
    with stack:
        shared = _shared_read(path, key)
        if shared is not _MISS:
            return shared
        payload = _spawn_dump(impl, args)
        if payload is not _CONTENDED:
            _shared_write(path, key, payload)
        return payload


def dump(impl, args=_DEFAULT_ARGS, *, in_process: bool = True):
    """The machine-local dump as a dict, or None when it is not fully answerable.

    A `.py` impl is read in-process (zero spawns). Otherwise: per-process mtime-keyed cache,
    then a box-wide single-flight around one bounded spawn. A lock-wait timeout or a killed
    child returns None and never spawns again; `in_process=False` forces the spawn path.
    """
    impl = str(impl)
    args = tuple(args)
    if in_process and impl.endswith(".py"):
        answered = _dump_in_process(impl, args)
        if answered is not None:
            return answered[0]
    key = _cache_key(impl, args)
    hit = _cache_get(key)
    if hit is not _MISS:
        return hit
    if not _PROCESS_FLIGHT.acquire(timeout=ML_WAIT_TIMEOUT_S):
        return None
    try:
        hit = _cache_get(key)
        if hit is not _MISS:
            return hit
        payload = _box_wide_flight(impl, args, key)
        if payload is _CONTENDED:
            return None
        _cache_put(key, payload)
        return payload
    finally:
        _PROCESS_FLIGHT.release()


def cached_dump(impl, args=_DEFAULT_ARGS):
    """The machine-local dump from caches and the in-process kernel only, else None.

    Order: per-process cache, in-process `.py` kernel, shared on-disk cache. Never spawns and
    never takes the box lock or a slot, so it is safe on an event-loop thread.
    """
    impl = str(impl)
    args = tuple(args)
    key = _cache_key(impl, args)
    hit = _cache_get(key)
    if hit is not _MISS:
        return hit
    if impl.endswith(".py"):
        answered = _dump_in_process(impl, args)
        if answered is not None:
            return answered[0]
    shared = _shared_read(_shared_cache_path(impl, args), key)
    return None if shared is _MISS else shared


def cached_get(impl, key: str):
    """(value | None, ok) from caches and the in-process kernel only; (None, False) on a miss.

    Same contract as `get` minus every spawn and lock: safe on an event-loop thread.
    """
    impl = str(impl)
    payload = cached_dump(impl)
    if payload is not None and key in payload:
        value = payload[key]
        return (value if isinstance(value, str) else None), True
    if impl.endswith(".py"):
        answered = _get_in_process(impl, key)
        if answered is not None:
            return answered
    return None, False


def _get_in_process(impl: str, key: str):
    try:
        mod = _load_kernel(impl)
        rc, val = mod.resolve_one(key, layers=None)
        if rc == mod.EXIT_OK:
            return (val if isinstance(val, str) else None, True)
        if rc == mod.EXIT_NOT_FOUND:
            return (None, True)
    except (SystemExit, Exception):
        pass
    return None


def get(impl, key: str, *, in_process: bool = True):
    """(value | None, ok): ok is False only when the read itself failed, not on a clean absence.

    A set-but-empty key reads "", never None; None means unset.

    A dump that fails or times out falls through to one `get` spawn, so the worst case is two
    bounded spawns (2 x ML_SPAWN_TIMEOUT_S); neither is retried.
    """
    impl = str(impl)
    payload = dump(impl, in_process=in_process)
    if payload is not None and key in payload:
        value = payload[key]
        return (value if isinstance(value, str) else None), True
    if in_process and impl.endswith(".py"):
        answered = _get_in_process(impl, key)
        if answered is not None:
            return answered
    # A failed or absent answer is remembered for CACHE_TTL_S like a dump's, so a stalled
    # impl costs one bounded `get` spawn per key per window, never one per call.
    ckey = _cache_key(impl, ("get", key))
    hit = _cache_get(ckey)
    if hit is not _MISS:
        return hit
    result = _get_spawned(impl, key)
    if result is _CONTENDED:
        return None, False
    _cache_put(ckey, result)
    return result


def _get_spawned(impl: str, key: str):
    cp = _run(_argv(impl, "get", key), ML_SPAWN_TIMEOUT_S)
    if cp is _CONTENDED:
        return _CONTENDED
    if cp is None:
        return None, False
    if cp.returncode == 0:
        return cp.stdout.rstrip("\r\n"), True
    if cp.returncode == 1:
        return None, True
    return None, False
