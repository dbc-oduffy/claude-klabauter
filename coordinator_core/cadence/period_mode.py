"""Period-mode resolver for the cadence ceremonies (day and week).

Answers "is this invocation catch-up or concluding, and which periods are missed".
Pure in-process arithmetic: thresholds come from the machine-local registry via
tomllib (zero spawns), memoised per process. The engine-mirror constants are
compared by DoE's parity test against its `cadence-period-mode-fragment.json`.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

CATCH_UP_FLAG = "--catch-up"
CONCLUDE_FLAG = "--conclude"
MODE_VALUES = ["catch_up", "conclude"]
SOURCE_VALUES = ["explicit", "auto-clock"]
DAY_CONCLUDE_AFTER_KEY = "coordinator.cadence.day_conclude_after"
DAY_CONCLUDE_AFTER_DEFAULT = "15:00"
WEEK_CONCLUDE_AFTER_DAYS_KEY = "coordinator.cadence.week_conclude_after_days"
WEEK_CONCLUDE_AFTER_DAYS_DEFAULT = 4
PERIOD_MODE_KEY = "period_mode"
NARRATION_LEAD_PREFIX = "Catch-up run: "

#: Ids a `catch_up` brief omits from `directives[]` (absent, never self-skipping).
CONCLUDING_DIRECTIVES: dict[str, tuple[str, ...]] = {
    "workday-complete": (
        "d_step9_changelog",
        "d_goal_close_day",
        "d_step10_5_post_command_hook",
        "jp_step4b_analyst_dispatch",
        "jp_step4_5_clustering_dispatch",
    ),
    "workweek-complete": (
        "d_step13_archive_close",
        "d_step13_5_post_command_hook",
        "jp_step8_5_loe_high_water",
        "jp_step9_editorial_bucketing",
        "jp_step9_pm_release_notes_gate",
        "jp_step10_semver_judgment",
        "jp_step10_5_gh_release_publish",
    ),
}


@dataclass(frozen=True)
class PeriodMode:
    mode: str
    source: str
    in_progress: str
    missed: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "source": self.source,
            "in_progress": self.in_progress,
            "missed": list(self.missed),
            "reason": self.reason,
        }


def period_flag_error(
    *, catch_up: bool, conclude: bool, only_mode: bool = False, for_date: str | None = None
) -> str | None:
    """The refusal for a `--catch-up`/`--conclude` combination the contract forbids, else `None`."""
    if catch_up and conclude:
        return f"{CATCH_UP_FLAG} and {CONCLUDE_FLAG} are mutually exclusive"
    flag = CATCH_UP_FLAG if catch_up else CONCLUDE_FLAG if conclude else None
    if flag and only_mode:
        return f"{flag} cannot be combined with --only"
    if flag and for_date:
        return f"{flag} cannot be combined with --for-date"
    return None


def local_now() -> datetime:
    """Now on the coordinator's day key (`daily_day.local_day`) and the machine's wall clock."""
    from coordinator_core.daily_day import local_day  # noqa: PLC0415

    return datetime.combine(date.fromisoformat(local_day()), datetime.now().time())


def week_window(d: date, week_anchor: date) -> tuple[date, date]:
    """`(start, end_exclusive)` of the 7-day window containing `d`, on `week_anchor`'s chain.

    Trap: anchor on `week_anchor`'s weekday, never Monday — repos differ
    (claude-klabauter's chain is Monday, DoE's is Tuesday) and a hardcoded weekday
    splits one repo's archive across two chains."""
    start = week_anchor + timedelta(days=7 * ((d - week_anchor).days // 7))
    return start, start + timedelta(days=7)


_REGISTRY_CACHE: dict[str, dict[str, object]] = {}


def _machine_local_dir() -> Path:
    home = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if not home:
        base = os.environ.get("CLAUDE_HOME") or str(Path.home())
        home = str(Path(base) / ".coordinator-claude-settings")
    return Path(home) / "machine-local"


def _registry_values() -> dict[str, object]:
    """Flat registry keys, `registry.local.toml` over `registry.toml`; unreadable files are skipped."""
    mdir = _machine_local_dir()
    cache_key = str(mdir)
    cached = _REGISTRY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    merged: dict[str, object] = {}
    for fname in ("registry.toml", "registry.local.toml"):
        try:
            data = tomllib.loads((mdir / fname).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        merged.update(data)
    _REGISTRY_CACHE[cache_key] = merged
    return merged


def _clear_registry_cache() -> None:
    _REGISTRY_CACHE.clear()


def _parse_hhmm(raw: object) -> tuple[int, int] | None:
    if not isinstance(raw, str):
        return None
    try:
        hh, mm = raw.strip().split(":")
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None


def _day_threshold(reasons: list[str]) -> tuple[int, int]:
    raw = _registry_values().get(DAY_CONCLUDE_AFTER_KEY)
    if raw is None:
        return _parse_hhmm(DAY_CONCLUDE_AFTER_DEFAULT)  # type: ignore[return-value]
    parsed = _parse_hhmm(raw)
    if parsed is None:
        reasons.append(
            f"{DAY_CONCLUDE_AFTER_KEY}={raw!r} unparseable; default {DAY_CONCLUDE_AFTER_DEFAULT} used"
        )
        return _parse_hhmm(DAY_CONCLUDE_AFTER_DEFAULT)  # type: ignore[return-value]
    return parsed


def _week_threshold(reasons: list[str]) -> int:
    raw = _registry_values().get(WEEK_CONCLUDE_AFTER_DAYS_KEY)
    if raw is None:
        return WEEK_CONCLUDE_AFTER_DAYS_DEFAULT
    if isinstance(raw, bool):
        value = None
    elif isinstance(raw, int):
        value = raw
    else:
        try:
            value = int(str(raw).strip())
        except ValueError:
            value = None
    if value is None or value < 0:
        reasons.append(
            f"{WEEK_CONCLUDE_AFTER_DAYS_KEY}={raw!r} unparseable; "
            f"default {WEEK_CONCLUDE_AFTER_DAYS_DEFAULT} used"
        )
        return WEEK_CONCLUDE_AFTER_DAYS_DEFAULT
    return value


def resolve_period_mode(
    cadence: str,
    now_local: datetime,
    explicit: str | None,
    *,
    week_anchor: date,
    concluded: Iterable[str] = (),
    since: date | None = None,
) -> PeriodMode:
    """Resolve `catch_up` vs `conclude` for one invocation of a day or week ceremony.

    `explicit` (a MODE_VALUES member) always wins. Otherwise the day is
    `catch_up` before the machine-local `day_conclude_after` clock time; the
    week is `catch_up` fewer than `week_conclude_after_days` days past the
    current week's start on `week_anchor`'s chain. `missed` lists, oldest first,
    every period from `since` (day default: none; week default: `week_anchor`'s
    week) up to but excluding the in-progress one that is not in `concluded`.
    An unparseable threshold falls back to its default and says so in `reason`.
    """
    if cadence not in ("day", "week"):
        raise ValueError(f"cadence must be 'day' or 'week', got {cadence!r}")
    if explicit is not None and explicit not in MODE_VALUES:
        raise ValueError(f"explicit must be one of {MODE_VALUES} or None, got {explicit!r}")

    done = set(concluded)
    today = now_local.date()
    reasons: list[str] = []

    if cadence == "day":
        in_progress = today
        first = since
        step = timedelta(days=1)
        cutoff = _day_threshold(reasons)
        auto_catch_up = (now_local.hour, now_local.minute) < cutoff
        auto_reason = f"local {now_local:%H:%M} vs {cutoff[0]:02d}:{cutoff[1]:02d}"
    else:
        in_progress = week_window(today, week_anchor)[0]
        first = since if since is not None else week_window(week_anchor, week_anchor)[0]
        step = timedelta(days=7)
        limit = _week_threshold(reasons)
        days_past = (today - in_progress).days
        auto_catch_up = days_past < limit
        auto_reason = f"{days_past} days past week start vs {limit}"

    missed: list[str] = []
    if first is not None:
        if cadence == "week":
            first = week_window(first, week_anchor)[0]
        p = first
        while p < in_progress:
            iso = p.isoformat()
            if iso not in done:
                missed.append(iso)
            p += step

    if explicit is not None:
        mode, source, reason = explicit, "explicit", f"explicit {explicit}"
    else:
        mode = "catch_up" if auto_catch_up else "conclude"
        source, reason = "auto-clock", auto_reason
    if reasons:
        reason = "; ".join([reason, *reasons])
    return PeriodMode(mode, source, in_progress.isoformat(), tuple(missed), reason)
