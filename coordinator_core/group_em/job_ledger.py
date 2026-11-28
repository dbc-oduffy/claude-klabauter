"""Fleet job ledger classifier: join running heavy processes to their GO grants and flag them.

Pure functions over already-read data (no I/O). ``is_heavy`` says which processes count;
``classify`` joins each heavy process to the grant booked for it (by ``pid`` plus the grant's
``pid_started_at``, directly or through a booked ancestor) and flags ``unbooked``, ``over_window``,
``behind_rate``; a grant whose process is gone is listed with ``exited``. Never raises.
"""

from __future__ import annotations

from typing import Iterable, Optional

SETTINGS_PREFIX = "group_em.job_ledger.heavy_"
# Heavy when ANY axis is at or above its threshold; settings keys are SETTINGS_PREFIX + axis.
DEFAULT_THRESHOLDS = {
    "cpu_pct": 50.0,
    "rss_mb": 2048.0,
    "io_bps": 20.0 * 1024 * 1024,
    "vram_mib": 512.0,
}
FLAG_UNBOOKED = "unbooked"
FLAG_OVER_WINDOW = "over_window"
FLAG_BEHIND_RATE = "behind_rate"
FLAG_EXITED = "exited"
# Two clocks read the same process start a little apart; a larger gap is a reused pid.
START_TOLERANCE_S = 2.0
_MAX_ANCESTRY = 64


def resolve_thresholds(settings: Optional[dict] = None) -> dict:
    """Thresholds from ``group_em.job_ledger.heavy_*`` keys of ``settings`` over the defaults."""
    out = dict(DEFAULT_THRESHOLDS)
    for axis in DEFAULT_THRESHOLDS:
        v = _num((settings or {}).get(SETTINGS_PREFIX + axis))
        if v is not None:
            out[axis] = v
    return out


def _num(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def is_heavy(proc: dict, thresholds: Optional[dict] = None) -> bool:
    """True when any of cpu_pct, rss_mb, io (read+write bytes/s) or vram_mib reaches its threshold."""
    if not isinstance(proc, dict):
        return False
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    io = (_num(proc.get("io_read_bps")) or 0.0) + (_num(proc.get("io_write_bps")) or 0.0)
    for value, axis in (
        (_num(proc.get("cpu_pct")), "cpu_pct"),
        (_num(proc.get("rss_mb")), "rss_mb"),
        (io or None, "io_bps"),
        (_num(proc.get("vram_mib")), "vram_mib"),
    ):
        if value is not None and value >= t[axis]:
            return True
    return False


def _same_process(grant: dict, proc: dict) -> bool:
    """The grant's pid is this process; a recorded start time that disagrees means a reused pid."""
    if grant.get("pid") != proc.get("pid"):
        return False
    recorded, seen = _num(grant.get("pid_started_at")), _num(proc.get("started_at"))
    if recorded is None or seen is None:
        return True
    return abs(recorded - seen) <= START_TOLERANCE_S


def _booked_grant(proc: dict, by_pid: dict, grants: list) -> Optional[dict]:
    """The grant booked on ``proc`` or on the nearest ancestor that is the booked process."""
    seen: set = set()
    cur: Optional[dict] = proc
    for _ in range(_MAX_ANCESTRY):
        if cur is None or cur.get("pid") in seen:
            return None
        seen.add(cur.get("pid"))
        for g in grants:
            if _same_process(g, cur):
                return g
        cur = by_pid.get(cur.get("ppid"))
    return None


def _row(proc: Optional[dict], grant: Optional[dict], now: float) -> dict:
    proc = proc or {}
    row = {
        "owner_sid": None,
        "go_id": None,
        "pid": proc.get("pid"),
        "cmdline": proc.get("cmdline"),
        "started_at": proc.get("started_at"),
        "window_s": None,
        "deadline": None,
        "expected_rate": None,
        "progress": None,
        "rate_so_far": None,
        "cpu_pct": proc.get("cpu_pct"),
        "rss_mb": proc.get("rss_mb"),
        "vram_mib": proc.get("vram_mib"),
        "io_read_bps": proc.get("io_read_bps"),
        "io_write_bps": proc.get("io_write_bps"),
        "flags": [],
    }
    if grant is None:
        row["flags"] = [FLAG_UNBOOKED]
        return row
    granted_at = _num(grant.get("granted_at"))
    window = _num(grant.get("window_s"))
    if window is None:
        window = _num(grant.get("ttl_s"))
    expected = _num(grant.get("expected_rate"))
    progress = _num(grant.get("progress"))
    elapsed = None if granted_at is None else now - granted_at
    rate = progress / elapsed if progress is not None and elapsed and elapsed > 0 else None
    deadline = granted_at + window if granted_at is not None and window is not None else None
    row.update(
        owner_sid=grant.get("owner_sid"),
        go_id=grant.get("run"),
        window_s=window,
        deadline=deadline,
        expected_rate=expected,
        progress=progress,
        rate_so_far=rate,
    )
    if row["pid"] is None:
        row["pid"] = grant.get("pid")
    if deadline is not None and now > deadline:
        row["flags"].append(FLAG_OVER_WINDOW)
    if expected is not None and rate is not None and rate < expected:
        row["flags"].append(FLAG_BEHIND_RATE)
    return row


def classify(
    procs: Iterable[dict],
    grants: Iterable[dict],
    now: float,
    thresholds: Optional[dict] = None,
) -> list[dict]:
    """One job row per heavy process and per pid-booked grant; ``flags`` names what is wrong.

    ``procs`` are proc_census rows (``pid, ppid, started_at, cmdline, cpu_pct, rss_mb, vram_mib,
    io_read_bps, io_write_bps``); ``grants`` are pid-booked grant rows. A heavy process matches the
    grant booked on its pid or on a booked ancestor; none gives ``["unbooked"]``. A booked grant
    with no heavy descendant is listed through its own process; one whose process is gone is
    listed with ``["exited"]`` alone. Rows: heavy processes first, then grants.
    """
    plist = [p for p in procs if isinstance(p, dict)]
    glist = [g for g in grants if isinstance(g, dict) and g.get("pid") is not None]
    by_pid = {p.get("pid"): p for p in plist}
    rows: list[dict] = []
    matched: set = set()
    for proc in plist:
        if not is_heavy(proc, thresholds):
            continue
        grant = _booked_grant(proc, by_pid, glist)
        if grant is not None:
            matched.add(id(grant))
        rows.append(_row(proc, grant, now))
    for grant in glist:
        if id(grant) in matched:
            continue
        own = next((p for p in plist if _same_process(grant, p)), None)
        row = _row(own, grant, now)
        if own is None:
            row["flags"] = [FLAG_EXITED]
        rows.append(row)
    return rows
