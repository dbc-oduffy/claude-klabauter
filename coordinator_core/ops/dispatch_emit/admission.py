"""
coordinator_core.ops.dispatch_emit.admission — load-aware workflow admission.

Reads live host CPU load and available RAM before a workflow is emitted onto
this box, and holds when the box is over a config-sourced threshold — the
in-process sampler PM ruling (DoE-claude
docs/research/2026-09-27-beat-vanilla-restructure/target-design.md § 7):
static concurrency caps are the wrong instrument; the emitter waits on the
box's LIVE state instead.

Three call sites, three postures:
  - `should_refuse_warm` — read-only, one shot, never sleeps. Used by
    `invoke_from_argv._run_entrypoint` on the warm leg to decide whether an
    emission-shaped invocation must leave the pool and re-run cold.
  - `await_admission(hold_allowed=True)` — the cold leg's single blocking
    wait: `cli.main`, re-run cold after a warm refusal, holds here until
    under threshold or `max_hold_s` elapses.
  - `await_admission(hold_allowed=False)` — the warm-served safety net:
    `cli.main` running inside a pool worker reads once and never holds,
    covering the race where load rises between the warm-leg read and
    emission.

Trap — the caller's tool timeout: the EM invokes the engine CLI through its
Bash tool, whose default timeout is 120 s. A configured `max_hold_s` at or
above that is killed by the harness mid-hold and reads as a hang, not a
graceful admit-at-ceiling. Keep the seeded `max_hold_s` well under 120 s.

Why the hold never runs in a pool worker: `emit-dispatch-workflow` is a warm
allowlisted entrypoint, served inside a shared pool worker under a 30 s door
deadline (docs/reference/warm-pool-carve-outs.md) — a multi-second hold
there would pin that slot away from every other queued caller, which is the
exact occupancy defect that document exists to prevent. The hold instead
runs only in the caller's own cold process tree (the -32007 re-run), where a
sleeping process holds no shared resource.

Config precedence, per key: machine-local `workflow_admission.<key>` (read
fresh on every call via `machine_resolver.merged_flat_registry` — zero
spawn, no process-lifetime cache) over the emitting repo's
`coordinator.local.md` frontmatter `workflow_admission:` map (found by a
pure upward path walk from `start_dir` to the nearest ancestor holding
`.git` — never a `git` spawn). Any of the four `CONFIG_KEYS` missing (or
unparseable / non-positive) after the merge is "unconfigured" for that key —
the whole admission is unconfigured (`admitted_unconfigured`) unless all
four resolve. No threshold default lives in code (AC3's AST-checked
negative spec).

Unreadable is not loaded: a reading whose relevant metrics are ALL `None`
never holds (`admitted_unreadable`) — a broken sampler must not stall every
emission for `max_hold_s`.

Hold loop (the ONE sleep call site in this module): jittered re-check,
±25 % around `recheck_s`, clipped to the remaining time before
`max_hold_s`'s deadline — de-synchronises concurrently-held emitters so they
do not all re-check, and admit, on the same tick. Known limit: emitters that
all reach `max_hold_s` still admit together; there is no cross-process
admission token.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.machine_resolver import merged_flat_registry
from coordinator_core.resolve_validation_cmd import cs_read_local_md_mapping
from coordinator_core.telemetry import op_latency

DISABLE_ENV = "COORDINATOR_WORKFLOW_ADMISSION_DISABLE"
CONFIG_KEYS = ("cpu_load_max", "mem_avail_pct_min", "max_hold_s", "recheck_s")

_LOCAL_MD_NAME = "coordinator.local.md"
_MACHINE_LOCAL_PREFIX = "workflow_admission."
_LOCAL_MD_MAPPING_KEY = "workflow_admission"


@dataclass(frozen=True)
class AdmissionThresholds:
    """Resolved four-key threshold set. `source` maps each of CONFIG_KEYS to
    which config layer supplied its value ("machine-local" or
    "coordinator.local.md") — carried into the admission record so a reader
    can see where a threshold came from without re-resolving config."""

    cpu_load_max: float
    mem_avail_pct_min: float
    max_hold_s: float
    recheck_s: float
    source: dict


def _find_repo_root(start_dir: Optional[Path]) -> Optional[Path]:
    """Pure upward path walk from `start_dir` (default cwd) to the nearest
    ancestor holding a `.git` entry (directory or file — a worktree's `.git`
    is a file). No `git` spawn. Returns None if none is found."""
    current = (Path(start_dir) if start_dir is not None else Path.cwd()).resolve()
    while True:
        git_entry = current / ".git"
        if git_entry.is_dir() or git_entry.is_file():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _parse_positive_float(raw) -> Optional[float]:
    """A malformed or non-positive value is treated as missing — never
    raised. Accepts an already-numeric value (defensive; config readers
    return strings) as well as a string."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def _load_thresholds_with_missing(
    start_dir: Optional[Path],
) -> tuple[Optional[AdmissionThresholds], list]:
    """Resolve the four CONFIG_KEYS per the module's precedence rule.
    Returns (thresholds, missing_keys) — thresholds is None unless all four
    keys resolve; missing_keys always names every key that did not."""
    machine_flat = merged_flat_registry()

    local_md_map: dict = {}
    repo_root = _find_repo_root(start_dir)
    if repo_root is not None:
        local_md_map = cs_read_local_md_mapping(str(repo_root), _LOCAL_MD_MAPPING_KEY)

    values: dict = {}
    source: dict = {}
    missing: list = []
    for key in CONFIG_KEYS:
        raw = machine_flat.get(_MACHINE_LOCAL_PREFIX + key)
        layer = "machine-local"
        if raw is None:
            raw = local_md_map.get(key)
            layer = "coordinator.local.md"
        parsed = _parse_positive_float(raw)
        if parsed is None:
            missing.append(key)
            continue
        values[key] = parsed
        source[key] = layer

    if missing:
        return None, missing

    return (
        AdmissionThresholds(
            cpu_load_max=values["cpu_load_max"],
            mem_avail_pct_min=values["mem_avail_pct_min"],
            max_hold_s=values["max_hold_s"],
            recheck_s=values["recheck_s"],
            source=source,
        ),
        [],
    )


def load_thresholds(start_dir: Optional[Path]) -> Optional[AdmissionThresholds]:
    """Resolve the admission thresholds for `start_dir`'s enclosing repo, or
    None when any of CONFIG_KEYS is unconfigured (missing, unparseable, or
    non-positive)."""
    thresholds, _missing = _load_thresholds_with_missing(start_dir)
    return thresholds


def _thresholds_dict(thresholds: AdmissionThresholds) -> dict:
    return {
        "cpu_load_max": thresholds.cpu_load_max,
        "mem_avail_pct_min": thresholds.mem_avail_pct_min,
        "max_hold_s": thresholds.max_hold_s,
        "recheck_s": thresholds.recheck_s,
        "source": dict(thresholds.source),
    }


def _reading_unreadable(reading: dict) -> bool:
    """Fully unreadable: no metric decide() could act on is present. A
    partially-readable reading (one metric present, the other None) is NOT
    unreadable — decide() simply skips the missing metric."""
    cpu = reading.get("cpu_load")
    mem_avail = reading.get("mem_avail_mb")
    mem_total = reading.get("mem_total_mb")
    return cpu is None and (mem_avail is None or mem_total is None)


def decide(reading: dict, t: AdmissionThresholds) -> list:
    """[] when under every configured threshold; else one reason string per
    metric over its threshold, naming the metric, the reading and the
    threshold. A metric whose reading is None is skipped (never trips) —
    see `_reading_unreadable` for the all-None case."""
    reasons = []
    cpu = reading.get("cpu_load")
    if cpu is not None and cpu > t.cpu_load_max:
        reasons.append(f"cpu_load {cpu} > cpu_load_max {t.cpu_load_max}")
    mem_avail = reading.get("mem_avail_mb")
    mem_total = reading.get("mem_total_mb")
    if mem_avail is not None and mem_total is not None and mem_total > 0:
        pct = mem_avail / mem_total * 100
        if pct < t.mem_avail_pct_min:
            reasons.append(
                f"mem_avail_pct {pct} < mem_avail_pct_min {t.mem_avail_pct_min}"
            )
    return reasons


def _route() -> str:
    return "warm" if op_latency.execution_route() == op_latency.WARM_SERVER else "cold"


def _record(
    verdict: str,
    route: str,
    first_reading,
    last_reading,
    reasons: list,
    waited_s: float,
    rechecks: int,
    thresholds: Optional[dict],
    missing_keys: list,
) -> dict:
    return {
        "verdict": verdict,
        "route": route,
        "first_reading": first_reading,
        "last_reading": last_reading,
        "reasons": reasons,
        "waited_s": waited_s,
        "rechecks": rechecks,
        "thresholds": thresholds,
        "missing_keys": missing_keys,
    }


def await_admission(
    start_dir: Optional[Path],
    *,
    hold_allowed: bool,
    read_load: Optional[Callable[[], dict]] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    rng: Callable[[], float] = random.random,
) -> dict:
    """One blocking wait, re-reading every `recheck_s`, until under
    threshold or `max_hold_s` elapses (only when `hold_allowed`); otherwise
    one read and no hold. Never raises. See module docstring for the three
    call postures."""
    route = _route()

    if os.environ.get(DISABLE_ENV) == "1":
        return _record("disabled", route, None, None, [], 0.0, 0, None, [])

    thresholds, missing_keys = _load_thresholds_with_missing(start_dir)
    if thresholds is None:
        return _record(
            "admitted_unconfigured", route, None, None, [], 0.0, 0, None, missing_keys
        )

    reader = read_load
    if reader is None:
        from coordinator_core.telemetry.host_sampler import read_load as reader

    first = reader()
    if _reading_unreadable(first):
        return _record(
            "admitted_unreadable",
            route,
            first,
            first,
            [],
            0.0,
            0,
            _thresholds_dict(thresholds),
            [],
        )

    reasons = decide(first, thresholds)
    if not reasons:
        return _record(
            "admitted", route, first, first, [], 0.0, 0, _thresholds_dict(thresholds), []
        )

    if not hold_allowed:
        return _record(
            "admitted_warm_no_hold",
            route,
            first,
            first,
            reasons,
            0.0,
            0,
            _thresholds_dict(thresholds),
            [],
        )

    deadline = monotonic() + thresholds.max_hold_s
    waited = 0.0
    rechecks = 0
    last = first
    last_reasons = reasons
    while last_reasons:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        interval = min(thresholds.recheck_s * (0.75 + rng() / 2), remaining)
        sleep(interval)
        waited += interval
        rechecks += 1
        last = reader()
        if _reading_unreadable(last):
            last_reasons = []
            break
        last_reasons = decide(last, thresholds)

    verdict = "admitted_at_max_hold" if last_reasons else "admitted_after_hold"
    return _record(
        verdict,
        route,
        first,
        last,
        last_reasons,
        waited,
        rechecks,
        _thresholds_dict(thresholds),
        [],
    )


def should_refuse_warm(
    start_dir: Optional[Path], *, read_load: Optional[Callable[[], dict]] = None
) -> Optional[dict]:
    """Read-only, one-shot, never sleeps. Returns a record only when
    admission is configured AND the box is currently loaded; None
    otherwise (unconfigured, disabled, unreadable, or under threshold). The
    caller (`invoke_from_argv`) raises `EntrypointNotWarmLoadableError` when
    this returns non-None. Never raises."""
    if os.environ.get(DISABLE_ENV) == "1":
        return None

    thresholds, _missing_keys = _load_thresholds_with_missing(start_dir)
    if thresholds is None:
        return None

    reader = read_load
    if reader is None:
        from coordinator_core.telemetry.host_sampler import read_load as reader

    reading = reader()
    if _reading_unreadable(reading):
        return None

    reasons = decide(reading, thresholds)
    if not reasons:
        return None

    return {
        "reasons": reasons,
        "reading": reading,
        "thresholds": _thresholds_dict(thresholds),
    }
