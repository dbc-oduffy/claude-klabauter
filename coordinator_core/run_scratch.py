"""Run-scratch contract: claim, finish and sweep directories under the box scratch or build root (docs/reference/run-scratch-contract.md)."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

MARK_FILE = ".owner.json"
SCRATCH = "scratch"
BUILD = "build"
KEEP_DEFAULT = 2
CLAIM_SWEEP_CAP = 8
MARKLESS_MAX_AGE_S = 24 * 3600
_STAMP_FMT = "%Y%m%dT%H%M%SZ"
_NAME_RE = re.compile(r"^(?P<slug>.+)\.(?P<ts>\d{8}T\d{6}Z)\.(?P<pid>\d+)$")
_KEYS = {SCRATCH: "paths.scratch_root", BUILD: "paths.build_root"}
_BAD_SEGMENT_CHARS = frozenset('/\\:\0')


def _machine_resolver() -> ModuleType | None:
    """``coordinator_core.machine_resolver`` when importable, else None (a vendored copy)."""
    try:
        from coordinator_core import machine_resolver
    except ImportError:
        return None
    return machine_resolver


def _registry_dir() -> Path:
    override = os.environ.get("MACHINE_LOCAL_REGISTRY_DIR")
    if override:
        return Path(override)
    home = os.environ.get("COORDINATOR_SETTINGS_HOME")
    base = Path(home) if home else Path.home() / ".coordinator-claude-settings"
    return base / "machine-local"


def _toml_get(key: str) -> str | None:
    """``key`` from registry.local.toml then registry.toml; nested table and flat dotted-key forms both read."""
    try:
        import tomllib
    except ImportError:
        return None
    section, _, leaf = key.partition(".")
    for name in ("registry.local.toml", "registry.toml"):
        try:
            data = tomllib.loads((_registry_dir() / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        nested = data.get(section)
        value = data.get(key)
        if value is None and isinstance(nested, dict):
            value = nested.get(leaf)
        if isinstance(value, str) and value:
            return value
    return None


def configured(key: str) -> str | None:
    """The machine-local value of ``key`` (env, local registry, registry), or None when unset or empty."""
    resolver = _machine_resolver()
    if resolver is not None:
        return resolver.registry_get(key) or None
    env = os.environ.get("MACHINE_LOCAL_" + key.upper().replace(".", "_"))
    return env or _toml_get(key)


def scratch_root(fallback=None) -> Path:
    """``paths.scratch_root`` (not created); unset yields ``fallback``, else ``<tempdir>/coordinator``."""
    value = configured(_KEYS[SCRATCH])
    if value:
        return Path(value)
    return Path(fallback) if fallback is not None else Path(tempfile.gettempdir()) / "coordinator"


def build_root(fallback=None) -> Path:
    """``paths.build_root`` (not created); unset yields ``scratch_root(fallback)``."""
    value = configured(_KEYS[BUILD])
    return Path(value) if value else scratch_root(fallback)


def _root_for(root: str) -> Path:
    if root == SCRATCH:
        return scratch_root()
    if root == BUILD:
        return build_root()
    raise ValueError(f"root must be 'scratch' or 'build': {root!r}")


def _segment(value, what: str) -> str:
    text = str(value)
    if not text or text in (".", "..") or _BAD_SEGMENT_CHARS & set(text):
        raise ValueError(f"{what} must be one path segment: {value!r}")
    return text


def _pid_alive_windows(pid: int) -> bool:
    """invariant: access denied (another user's pid) is doubt and reads alive."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = k32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259
    finally:
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle(handle)


def _pid_alive_posix(pid: int) -> bool:
    """trap: signal 0 only probes on POSIX; ``os.kill(pid, 0)`` terminates on Windows."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def pid_alive(pid) -> bool:
    """False for a non-numeric, non-positive or exited pid; True when the answer is doubtful."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        return _pid_alive_windows(pid) if os.name == "nt" else _pid_alive_posix(pid)
    except Exception:
        return True


def _proc_start_linux(pid: int) -> str:
    with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
        tail = fh.read().rpartition(")")[2].split()
    return tail[19]


def _proc_start_windows(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = k32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle(handle)


def _proc_start_darwin(pid: int) -> str | None:
    import ctypes
    import ctypes.util
    import struct

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) != 0 or size.value < 16:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value < 16:
        return None
    sec, usec = struct.unpack_from("@qi", buf.raw, 0)
    return f"{sec}.{usec:06d}"


def proc_start(pid: int | str | None) -> str | None:
    """The process start token of ``pid`` as a string, or None where it cannot be read (no subprocess)."""
    try:
        pid = int(pid or 0)
        if pid <= 0:
            return None
        if sys.platform == "win32":
            return _proc_start_windows(pid)
        if sys.platform.startswith("linux"):
            return _proc_start_linux(pid)
        if sys.platform == "darwin":
            return _proc_start_darwin(pid)
    except Exception:
        return None
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_mark(directory: Path, mark: dict) -> None:
    tmp = directory / f"{MARK_FILE}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(mark, sort_keys=True), encoding="utf-8")
    os.replace(tmp, directory / MARK_FILE)


def read_mark(directory) -> dict | None:
    """The parsed ``.owner.json`` of ``directory``, or None when absent, unreadable or not an object."""
    try:
        data = json.loads((Path(directory) / MARK_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


_read_mark = read_mark


def _is_claim_mark(mark: dict | None) -> bool:
    return mark is not None and "procStart" in mark and "complete" in mark


def _claim_mark(pid: int, root: str, complete: bool = False, started: str | None = None) -> dict:
    stamp = started or _now().isoformat()
    return {
        "pid": pid,
        "procStart": proc_start(pid),
        "startedAt": stamp,
        "complete": complete,
        "root": root,
        "owner_kind": "pid",
        "owner_id": str(pid),
        "created": stamp,
        "repo": None,
        "ttl_hours": None,
    }


def _clear_readonly_and_retry(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    func(path)


def _remove(path: Path) -> bool:
    """Best-effort delete; a dir that survives is re-marked dead so the next sweep retries it. Never raises."""
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, ignore_errors=False, onexc=_clear_readonly_and_retry)
        else:
            shutil.rmtree(path, ignore_errors=False, onerror=_clear_readonly_and_retry)
    except OSError:
        pass
    if not path.exists():
        return True
    try:
        _write_mark(path, _claim_mark(0, SCRATCH))
    except OSError:
        pass
    return False


def owner_alive(mark: dict) -> bool:
    """False when the mark's pid has exited or now belongs to a process with a different start token."""
    pid = mark.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or not pid_alive(pid):
        return False
    recorded = mark.get("procStart")
    if recorded:
        current = proc_start(pid)
        if current is not None and current != recorded:
            return False
    return True


def _held(keep_if: Callable[[Path], bool] | None, path: Path) -> bool:
    """invariant: a ``keep_if`` that raises holds the entry."""
    if keep_if is None:
        return False
    try:
        return bool(keep_if(path))
    except Exception:
        return True


def sweep_kind(
    kind_dir, keep: int = KEEP_DEFAULT, cap: int | None = None, keep_if: Callable[[Path], bool] | None = None
) -> list[str]:
    """`sweep` of one ``<root>/<repo>/<kind>/`` dir; an entry ``keep_if`` holds is live and never counts against ``keep``."""
    kind_dir = Path(kind_dir)
    try:
        entries = [e for e in os.scandir(kind_dir) if _NAME_RE.match(e.name) and e.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    doomed: list[Path] = []
    complete: list[tuple[str, int, Path]] = []
    for entry in entries:
        path = Path(entry.path)
        mark = read_mark(path)
        if _held(keep_if, path):
            continue
        if mark is None:
            try:
                if time.time() - entry.stat().st_mtime > MARKLESS_MAX_AGE_S:
                    doomed.append(path)
            except OSError:
                pass
        elif not _is_claim_mark(mark):
            continue
        elif mark.get("complete") is True:
            head, _, pid = entry.name.rpartition(".")
            complete.append((head.rpartition(".")[2], int(pid), path))
        elif not owner_alive(mark):
            doomed.append(path)
    complete.sort(reverse=True)
    doomed.extend(p for _, _, p in complete[max(keep, 0):])
    removed: list[str] = []
    for path in doomed[: cap if cap is not None else len(doomed)]:
        if _remove(path):
            removed.append(str(path))
    return removed


_sweep_kind = sweep_kind


def sweep(
    root, keep: int = KEEP_DEFAULT, cap: int | None = None, keep_if: Callable[[Path], bool] | None = None
) -> list[str]:
    """Delete dead, excess and stale-markless claims under ``<root>/<repo>/<kind>/``; return removed paths (spec doc)."""
    removed: list[str] = []
    try:
        for repo in os.scandir(root):
            if not repo.is_dir(follow_symlinks=False):
                continue
            for kind in os.scandir(repo.path):
                if not kind.is_dir(follow_symlinks=False):
                    continue
                left = None if cap is None else cap - len(removed)
                if left is not None and left <= 0:
                    return removed
                removed += sweep_kind(kind.path, keep, left, keep_if)
    except OSError:
        pass
    return removed


def sweep_boxes(keep: int = KEEP_DEFAULT) -> list[str]:
    """`sweep` over the scratch and build roots (one pass when they coincide); returns removed paths."""
    removed: list[str] = []
    for root in sorted({str(scratch_root()), str(build_root())}):
        removed += sweep(root, keep=keep)
    return removed


def claim(repo: str, kind: str, slug: str, root: str = SCRATCH, pid: int | None = None) -> Path:
    """Create ``<root>/<repo>/<kind>/<slug>.<UTC yyyymmddTHHMMSSZ>.<pid>`` owned by ``pid`` (default this process)."""
    base = _root_for(root)
    kind_dir = base / _segment(repo, "repo") / _segment(kind, "kind")
    slug = _segment(slug, "slug")
    kind_dir.mkdir(parents=True, exist_ok=True)
    sweep_kind(kind_dir, KEEP_DEFAULT, CLAIM_SWEEP_CAP)
    owner = os.getpid() if pid is None else int(pid)
    start = _now()
    for bump in range(120):
        path = kind_dir / f"{slug}.{(start + timedelta(seconds=bump)).strftime(_STAMP_FMT)}.{owner}"
        try:
            path.mkdir()
        except FileExistsError:
            continue
        _write_mark(path, _claim_mark(owner, root, started=start.isoformat()))
        return path
    raise FileExistsError(f"no free claim name under {kind_dir}")


def finish(path, keep: int = KEEP_DEFAULT, keep_if: Callable[[Path], bool] | None = None) -> dict:
    """End a claim: a scratch claim is deleted; a build claim is marked complete and its kind dir pruned to ``keep``."""
    target = Path(path)
    if not _NAME_RE.match(target.name):
        raise ValueError(f"not a run_scratch claim: {target}")
    mark = read_mark(target)
    if mark is None or mark.get("root") != BUILD:
        return {"path": str(target), "action": "deleted", "removed": [str(target)] if _remove(target) else []}
    done = dict(mark)
    done["complete"] = True
    done["completedAt"] = _now().isoformat()
    _write_mark(target, done)
    return {"path": str(target), "action": "completed", "removed": sweep_kind(target.parent, keep, None, keep_if)}


def _emit(payload: dict) -> int:
    print(json.dumps(payload))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m coordinator_core.run_scratch")
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("root")
    p_claim = sub.add_parser("claim")
    p_claim.add_argument("repo")
    p_claim.add_argument("kind")
    p_claim.add_argument("slug")
    p_claim.add_argument("--root", choices=(SCRATCH, BUILD), default=SCRATCH)
    p_claim.add_argument("--pid", type=int, default=None, help="owner pid; default is the invoking parent process")
    p_finish = sub.add_parser("finish")
    p_finish.add_argument("path")
    p_finish.add_argument("--keep", type=int, default=KEEP_DEFAULT)
    p_sweep = sub.add_parser("sweep")
    p_sweep.add_argument("--root", choices=(SCRATCH, BUILD, "both"), default="both")
    p_sweep.add_argument("--keep", type=int, default=KEEP_DEFAULT)
    args = parser.parse_args(argv)
    try:
        if args.verb == "root":
            return _emit({SCRATCH: str(scratch_root()), BUILD: str(build_root())})
        if args.verb == "claim":
            owner = args.pid if args.pid is not None else os.getppid()
            return _emit({"path": str(claim(args.repo, args.kind, args.slug, args.root, pid=owner))})
        if args.verb == "finish":
            return _emit(finish(args.path, args.keep))
        names = (SCRATCH, BUILD) if args.root == "both" else (args.root,)
        roots = {str(_root_for(n)) for n in names}
        return _emit({"removed": [p for r in sorted(roots) for p in sweep(r, keep=args.keep)]})
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
