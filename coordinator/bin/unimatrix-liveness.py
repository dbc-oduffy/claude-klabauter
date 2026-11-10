#!/usr/bin/env python3
"""Spawn-free liveness for the unimatrix autolink supervisors: stdlib only, no subprocess.

Loaded by path (importlib spec_from_file_location). `proc_start` reproduces `ps -o lstart=` byte
for byte on POSIX (one-digit day space-padded) because recorded procStart strings are compared as text.
Boxes cache `autolink-boxes.json`: a json object with an "aliases" list; its mtime is the cache age.
"""
import json
import os
import struct
import sys
import time
from pathlib import Path

CACHE_NAME = "autolink-boxes.json"
CACHE_MAX_AGE = 24 * 3600
BACKOFF_BASE = 60
BACKOFF_CAP = 3600
ROLES = ("bridge", "inbox")
STILL_ACTIVE = 259
_PROC_PIDTBSDINFO = 3
_BSDINFO_SIZE = 136
_BSDINFO_TVSEC_OFFSET = 120


def settings_home():
    env = os.environ.get("COORDINATOR_SETTINGS_HOME")
    return Path(env) if env else Path.home() / ".coordinator-claude-settings"


def state_dir(home=None):
    return Path(home if home is not None else settings_home()) / "unimatrix"


def _lstart(epoch):
    t = time.localtime(epoch)
    return time.strftime("%a %b ", t) + f"{t.tm_mday:2d}" + time.strftime(" %H:%M:%S %Y", t)


def _darwin_start(pid):
    import ctypes
    import ctypes.util
    lib = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)
    buf = ctypes.create_string_buffer(_BSDINFO_SIZE)
    got = lib.proc_pidinfo(ctypes.c_int(pid), ctypes.c_int(_PROC_PIDTBSDINFO), ctypes.c_uint64(0),
                           buf, ctypes.c_int(_BSDINFO_SIZE))
    if got != _BSDINFO_SIZE:
        raise OSError(f"proc_pidinfo({pid}) returned {got}")
    (sec,) = struct.unpack_from("<Q", buf.raw, _BSDINFO_TVSEC_OFFSET)
    return _lstart(sec)


def _linux_start(pid):
    with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
        stat = fh.read()
    ticks = int(stat[stat.rindex(")") + 2:].split()[19])
    with open("/proc/stat", encoding="utf-8") as fh:
        btime = next(int(ln.split()[1]) for ln in fh if ln.startswith("btime "))
    return _lstart(btime + ticks // os.sysconf("SC_CLK_TCK"))


def _win_filetime(pid):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        raise OSError(f"cannot open pid {pid}")
    times = [wintypes.FILETIME() for _ in range(4)]
    ok = k32.GetProcessTimes(h, *[ctypes.byref(t) for t in times])
    k32.CloseHandle(h)
    if not ok:
        raise OSError(f"GetProcessTimes failed for pid {pid}")
    return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)


def proc_start(pid, platform=None):
    """Process-start stamp as the harness records it; raises OSError when the pid cannot be read."""
    plat = platform or sys.platform
    try:
        if plat == "win32":
            return _win_filetime(pid)
        if plat == "darwin":
            return _darwin_start(pid)
        return _linux_start(pid)
    except (ValueError, StopIteration, IndexError, AttributeError, struct.error) as ex:
        raise OSError(f"cannot read start of pid {pid}: {type(ex).__name__}") from ex


def pid_alive(pid):
    if pid <= 0:  # os.kill(0|-n, 0) would signal a process group
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = wintypes.DWORD()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def supervisor_live(rec):
    """True when the recorded pid is alive and is the same process that was started."""
    pid = rec.get("pid") if isinstance(rec, dict) else None
    if not isinstance(pid, int) or isinstance(pid, bool) or not pid_alive(pid):
        return False
    try:
        return proc_start(pid) == rec.get("procStart")
    except OSError:
        return False


def entry_live(d):
    """True when the entry's pid is alive and, if it records a procStart, is still that process.

    No recorded procStart, or an unreadable start, never proves staleness: pid liveness decides."""
    pid = d.get("pid") if isinstance(d, dict) else None
    if not isinstance(pid, int) or isinstance(pid, bool) or not pid_alive(pid):
        return False
    recorded = d.get("procStart")
    if not isinstance(recorded, str) or not recorded:
        return True
    try:
        return proc_start(pid) == recorded
    except OSError:
        return True


def codex_holder_live(row):
    """Liveness of the Codex process a peer row recorded as `codexPid`/`codexProcStart`.

    True: holder alive and still that process. False: holder dead or its pid reused.
    None: the row records no holder, so liveness is unknown. An unreadable start is not staleness."""
    pid = row.get("codexPid") if isinstance(row, dict) else None
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    return entry_live({"pid": pid, "procStart": row.get("codexProcStart")})


def codex_thread_holder(codex_home, thread_id):
    """The `codexPid`/`codexProcStart` of the Codex process that last logged `thread_id`; {} when unknown.

    Reads the newest `process_uuid` ("pid:<n>:<uuid>") for the thread across `logs_*.sqlite`, read-only."""
    import re
    import sqlite3
    from contextlib import closing
    best = None
    for db in sorted(Path(codex_home).glob("logs_*.sqlite")):
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as conn:
                row = conn.execute("SELECT ts, ts_nanos, process_uuid FROM logs WHERE thread_id = ? "
                                   "AND process_uuid IS NOT NULL ORDER BY ts DESC, ts_nanos DESC LIMIT 1",
                                   (thread_id,)).fetchone()
        except (sqlite3.Error, ValueError, OSError):
            continue
        if row and (best is None or row[:2] > best[:2]):
            best = row
    match = re.match(r"pid:(\d+):", best[2]) if best else None
    if not match or int(match.group(1)) <= 0:
        return {}
    pid = int(match.group(1))
    holder = {"codexPid": pid}
    try:
        holder["codexProcStart"] = proc_start(pid)
    except OSError:
        pass
    return holder


def codex_locks_dir():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "thread-writer-locks"


def _peer_rows(peers_file):
    try:
        with open(peers_file, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    rows = data.get("peers") if isinstance(data, dict) else data
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def codex_probe_lines(peers_file, locks_dir):
    """One `name target holder-pid cwd live|stale` line per Codex peer row, then per lock-only thread.

    Lock-only threads have no peers-file row, hence no recorded holder: they print stale."""
    lines, seen = [], set()
    for row in _peer_rows(peers_file):
        if row.get("vendor") != "codex":
            continue
        target = str(row.get("target", ""))
        seen.add(target)
        pid = row.get("codexPid")
        shown = pid if isinstance(pid, int) and not isinstance(pid, bool) else "-"
        verdict = "live" if codex_holder_live(row) is True else "stale"
        lines.append(f"{row.get('name', '-')} {target or '-'} {shown} {row.get('cwd') or '-'} {verdict}")
    try:
        names = sorted(os.listdir(locks_dir))
    except OSError:
        names = []
    for f in names:
        ident = f[:-len(".lock")]
        if f.startswith(".") or not f.endswith(".lock") or not ident or ident in seen:
            continue
        lines.append(f"codex-{ident} {ident} - - stale")
    return lines


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "codex-probe":
        print("usage: unimatrix-liveness.py codex-probe [--peers FILE] [--locks-dir DIR]", file=sys.stderr)
        return 2
    opts = {"--peers": state_dir() / "peers.json", "--locks-dir": codex_locks_dir()}
    rest = args[1:]
    while rest:
        if rest[0] not in opts or len(rest) < 2:
            print(f"unimatrix-liveness.py codex-probe: bad argument {rest[0]!r}", file=sys.stderr)
            return 2
        opts[rest[0]] = Path(rest[1])
        rest = rest[2:]
    for line in codex_probe_lines(opts["--peers"], opts["--locks-dir"]):
        print(line)
    return 0


def read_record(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _slug(alias):
    return alias.replace("@", "_at_")


def healthy(state_dir, now=None):
    """True only when the boxes cache is under 24h old and every listed alias has both supervisors live.

    A fresh cache listing zero aliases is healthy; a missing or unreadable cache is not."""
    sdir = Path(state_dir)
    cache = sdir / CACHE_NAME
    now = time.time() if now is None else now
    try:
        age = now - cache.stat().st_mtime
    except OSError:
        return False
    if age >= CACHE_MAX_AGE:
        return False
    aliases = read_record(cache).get("aliases")
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        return False
    for alias in aliases:
        rec = read_record(sdir / f"autolink-{_slug(alias)}.json")
        if not all(supervisor_live(rec.get(role) or {}) for role in ROLES):
            return False
    return True


def _backoff_path(state_dir, alias):
    return Path(state_dir) / f"autolink-{_slug(alias)}.backoff.json"


def backoff_until(state_dir, alias):
    """Epoch before which `alias` is not probed; 0 when no backoff is recorded."""
    until = read_record(_backoff_path(state_dir, alias)).get("until")
    return float(until) if isinstance(until, (int, float)) and not isinstance(until, bool) else 0.0


def note_probe(state_dir, alias, ok, now=None):
    """Record a probe outcome: success removes the backoff, failure doubles it (60s base, 3600s cap)."""
    path = _backoff_path(state_dir, alias)
    if ok:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    now = time.time() if now is None else now
    prior = read_record(path).get("failures")
    failures = (prior if isinstance(prior, int) and not isinstance(prior, bool) and prior > 0 else 0) + 1
    delay = min(BACKOFF_BASE * 2 ** (failures - 1), BACKOFF_CAP)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"failures": failures, "until": now + delay}), encoding="utf-8")
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())
