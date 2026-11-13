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


def run_bounded(argv, *, timeout: float = ML_SPAWN_TIMEOUT_S):
    """Run argv once under a hard kill ceiling.

    Returns the CompletedProcess, or None when the child could not start or outlived the
    ceiling; on expiry the whole process tree is killed and the direct child reaped before
    return. Never retries: None means the caller takes its existing fallback, never a second
    spawn of the same read. `timeout` is clamped to ML_SPAWN_TIMEOUT_S.
    """
    timeout = min(float(timeout), ML_SPAWN_TIMEOUT_S)
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
    except (OSError, ValueError):
        return None
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _reap(proc)
        return None
    except BaseException:
        _reap(proc)
        raise
    return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)


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
    root = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if not root:
        home = os.environ.get("CLAUDE_HOME") or str(Path.home())
        root = os.path.join(home, ".coordinator-claude-settings")
    return os.path.join(root, "machine-local")


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


def _dump_in_process(impl: str, args: tuple):
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
    cp = run_bounded(_argv(impl, "dump", *args))
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
    """One dump per registry state across every process on the box; waiters read its result."""
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
        path = _shared_cache_path(impl, args)
        shared = _shared_read(path, key)
        if shared is not _MISS:
            return shared
        payload = _spawn_dump(impl, args)
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
        _cache_put(key, payload)
        return payload
    finally:
        _PROCESS_FLIGHT.release()


def _get_in_process(impl: str, key: str):
    try:
        mod = _load_kernel(impl)
        rc, val = mod.resolve_one(key, layers=None)
        if rc == mod.EXIT_OK:
            return (val if val else None, True)
        if rc == mod.EXIT_NOT_FOUND:
            return (None, True)
    except (SystemExit, Exception):
        pass
    return None


def get(impl, key: str, *, in_process: bool = True):
    """(value | None, ok): ok is False only when the read itself failed, not on a clean absence.

    A dump that fails or times out falls through to one `get` spawn, so the worst case is two
    bounded spawns (2 x ML_SPAWN_TIMEOUT_S); neither is retried.
    """
    impl = str(impl)
    payload = dump(impl, in_process=in_process)
    if payload is not None and key in payload:
        value = payload[key]
        return (value if isinstance(value, str) and value else None), True
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
    _cache_put(ckey, result)
    return result


def _get_spawned(impl: str, key: str):
    cp = run_bounded(_argv(impl, "get", key))
    if cp is None:
        return None, False
    if cp.returncode == 0:
        return (cp.stdout.strip() or None), True
    if cp.returncode == 1:
        return None, True
    return None, False
