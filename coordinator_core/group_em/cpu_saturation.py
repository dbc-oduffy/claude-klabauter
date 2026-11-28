"""cpu_saturation: pure decision helpers for the box CPU-saturation guard.

Busy-percent streak, the wake line, the CPU-orphan predicate, thresholds from the machine
registry, and the `contain` signature `cpu_contain` implements. Everything is injectable and
never raises; the caller (watch.py) supplies the sampled busy percent, process rows, the
orphan predicate and the protected list, so `coordinator_core/box_memory` imports nothing from
`coordinator_core/group_em`.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol

from coordinator_core.box_memory import pressure

KEY_BUSY_PCT = "box.saturation_busy_pct"
KEY_TICKS = "box.saturation_ticks"
KEY_PROC_CORE_PCT = "box.saturation_proc_core_pct"
KEY_PROTECTED = "box.saturation_protected"

DEFAULT_BUSY_PCT = 90.0
DEFAULT_TICKS = 3
DEFAULT_PROC_CORE_PCT = 80.0
DEFAULT_PROTECTED = (
    "system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "svchost.exe", "explorer.exe", "dwm.exe",
    "launchd", "kernel_task", "windowserver", "systemd", "init",
    "claude", "claude.exe", "codex", "codex.exe",
)
MIN_ORPHAN_AGE_S = 5 * 60


def next_state(prev: Any, busy_pct: Optional[float], threshold: float, n: int = 3) -> tuple[dict, bool]:
    """Advance the over-threshold streak; fire exactly once when it reaches `n`.

    State is `{"count": int}`. A missing or malformed `prev` is the initial state; a None or
    non-numeric `busy_pct` resets without firing; `busy_pct < threshold` re-arms (at or above
    the threshold counts)."""
    count = 0
    if isinstance(prev, dict):
        raw = prev.get("count")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            count = raw
    try:
        over = busy_pct is not None and not isinstance(busy_pct, bool) and float(busy_pct) >= float(threshold)
    except (TypeError, ValueError):
        over = False
    if not over:
        return {"count": 0}, False
    count += 1
    return {"count": count}, count == n


def saturation_line(alias: str, busy: Optional[float], ticks: int, top: Any) -> str:
    """One `SATURATION` wake line naming the box and its top processes. `top` is a list of
    `{name|comm, pid, cpu|pcpu}` dicts or preformatted strings; anything else renders as none."""
    parts = []
    for p in top if isinstance(top, (list, tuple)) else ():
        if isinstance(p, dict):
            name = p.get("name") or p.get("comm") or "?"
            cpu = p.get("cpu", p.get("pcpu", 0))
            pid = p.get("pid")
            parts.append(f"{name}[{pid}]({cpu}%)" if pid is not None else f"{name}({cpu}%)")
        else:
            parts.append(str(p))
    busy_txt = f"{busy:.0f}%" if isinstance(busy, (int, float)) and not isinstance(busy, bool) else "?"
    return (
        f"SATURATION box={alias} busy={busy_txt} ticks={ticks} "
        f"top: {', '.join(parts) or 'none'}"
    )


def is_cpu_orphan(
    row: Any,
    by_pid: dict,
    primitives: Any = None,
    now: Optional[int] = None,
    cpu_share: Optional[float] = None,
    min_core_pct: float = DEFAULT_PROC_CORE_PCT,
) -> bool:
    """True when `row` is a CPU-bound orphan: parent verified dead (`pressure.parent_dead`, the
    `is_aged_orphan` proof without its name restriction), age at least `MIN_ORPHAN_AGE_S`, and
    `cpu_share` (percent of one core) at least `min_core_pct`. Unreadable ctime reads as young;
    an absent `cpu_share` is False."""
    try:
        ctime = getattr(row, "ctime", 0)
        if not (ctime and ctime > 0) or cpu_share is None or isinstance(cpu_share, bool):
            return False
        if float(cpu_share) < float(min_core_pct):
            return False
        from coordinator_core.bash_guards._host_probe import ctime_units_per_second

        current = pressure.now_ctime() if now is None else now
        if current - ctime < MIN_ORPHAN_AGE_S * ctime_units_per_second():
            return False
        return pressure.parent_dead(row, by_pid, primitives)
    except Exception:
        return False


def thresholds(registry: Optional[dict] = None) -> dict:
    """`{busy_pct, ticks, proc_core_pct, protected}` from the merged machine registry, each with
    its coded default when the key is absent or unparsable. `registry` is injectable; None reads
    `machine_resolver.merged_flat_registry`."""
    if registry is None:
        try:
            from coordinator_core import machine_resolver

            registry = machine_resolver.merged_flat_registry()
        except Exception:
            registry = {}
    reg = registry if isinstance(registry, dict) else {}

    def _num(key: str, default: float, cast: Callable, floor: float) -> Any:
        try:
            v = cast(reg[key])
        except (KeyError, TypeError, ValueError):
            return default
        return v if v > floor else default

    raw = reg.get(KEY_PROTECTED)
    if isinstance(raw, str):
        raw = raw.split(",")
    names = [str(x).strip().lower() for x in raw if str(x).strip()] if isinstance(raw, (list, tuple)) else []
    return {
        "busy_pct": _num(KEY_BUSY_PCT, DEFAULT_BUSY_PCT, float, 0),
        "ticks": _num(KEY_TICKS, DEFAULT_TICKS, int, 0),
        "proc_core_pct": _num(KEY_PROC_CORE_PCT, DEFAULT_PROC_CORE_PCT, float, 0),
        "protected": tuple(names) if names else DEFAULT_PROTECTED,
    }


class ContainFn(Protocol):
    """Signature `cpu_contain.contain` implements (C4): `contain(rows, primitives, is_orphan,
    protected, saturated) -> list[action]`. `is_orphan(row) -> bool` and `protected` (lowercase
    names) are injected by the caller; with `saturated` False it returns `[]`. Each action is a
    dict `{pid, name, action, ok}`."""

    def __call__(
        self,
        rows: list,
        primitives: Any,
        is_orphan: Callable[[Any], bool],
        protected: Any,
        saturated: bool,
    ) -> list[dict]: ...
