"""Export lifecycle: the size cap checked before an export is built and the orphan sweep that
reclaims exports no live or parked run references. In-process file I/O only: no spawn."""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

CAP_ENV = "COORDINATOR_EXPORT_CAP_BYTES"
_DEFAULT_CAP_BYTES = 2 * 1024**3
EXPORT_SIZE_CAP_BYTES: int = _DEFAULT_CAP_BYTES
EXPORT_DIR_NAMES = ("export", "export-head", "export.base")

_FREE_DISK_FACTOR = 2
_TEST_BASE_RE = re.compile(r"^test-base-[0-9a-f]{8}-(\d+)$")


def _cap_bytes() -> int:
    raw = os.environ.get(CAP_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return EXPORT_SIZE_CAP_BYTES


def estimate_head_bytes(root: Path) -> Optional[int]:
    """Sum of tracked-entry sizes in `root`'s index, from one read of `.git/index`; None when the
    index is absent, unreadable, or v4."""
    from coordinator_core.git.git_index import parse_index_stat

    try:
        entries = parse_index_stat(root)
    except Exception:
        return None
    if not entries:
        return None
    return sum(int(e.size) for e in entries.values())


def check_export_cap(root: Path) -> None:
    """Raise `LandingExportError` naming the estimate and the cap when an export of `root` would
    exceed the cap, or when free disk is under twice the estimate. An unreadable estimate leaves the
    disk-free check, sized against the cap."""
    from coordinator_core.ops.dispatch_emit.landing_export import LandingExportError

    cap = _cap_bytes()
    estimate = estimate_head_bytes(root)
    if estimate is not None and estimate > cap:
        raise LandingExportError(f"export refused: estimated {estimate} bytes exceeds cap {cap} bytes")
    need = _FREE_DISK_FACTOR * (estimate if estimate is not None else cap)
    probe = Path(root)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return
    if free < need:
        shown = estimate if estimate is not None else f"unknown (cap {cap})"
        raise LandingExportError(
            f"export refused: free disk {free} bytes is under twice the estimate {shown} (cap {cap} bytes)"
        )


def _read_object(path: Path):
    """(found, dict|None): found False when the file is absent; dict None when it exists unreadable."""
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, None
    except (OSError, ValueError):
        return True, None
    return True, data if isinstance(data, dict) else None


def _parse_when(value) -> Optional[datetime]:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _retest_live(root: Path, run_id: str) -> bool:
    """True when a `running` retest record is inside its deadline or cannot be read."""
    from coordinator_core.ops.dispatch_emit import landing_retest

    found, record = _read_object(landing_retest.fresh_verdict_path(root, run_id))
    if not found:
        return False
    if record is None:
        return True
    if record.get("status") != landing_retest.RETEST_RUNNING:
        return False
    deadline = _parse_when(record.get("deadline"))
    return deadline is not None and datetime.now(timezone.utc) < deadline


def _run_referenced(root: Path, run_id: str) -> bool:
    """True when a live or parked run references `run_id`'s exports; an unreadable predicate counts
    as referenced."""
    from coordinator_core.ops.dispatch_emit import ask_contract, run_token
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED
    from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE, STATUS_LANDED, STATUS_SUPERSEDED

    try:
        record = ask_contract.record_dir(root, run_id)
        found, marker = _read_object(record / HALTED_FILE)
        if found and marker is None:
            return True
        if found and marker.get("status") == STATUS_HALTED:
            return True
        if _retest_live(root, run_id):
            return True
        if found:
            return marker.get("status") not in (STATUS_LANDED, STATUS_SUPERSEDED)
        if not record.is_dir():
            return False
        stage_found, stage = _read_object(record / ask_contract.RUN_STAGE_FILE)
        if not stage_found or stage is None:
            return True
        last = _parse_when(stage.get("last_activity_at"))
        if last is None:
            return True
        return (datetime.now(timezone.utc) - last).total_seconds() < run_token.TTL_SECONDS
    except Exception:
        return True


def sweep_orphan_exports(root: Path, *, keep_run: Optional[str] = None) -> list[str]:
    """Remove `scratch/warp/<run>/{export,export-head,export.base}` of every run no live or parked
    run references, and `scratch/test-base-<sha8>-<pid>` of a dead pid. `keep_run` is always
    spared. Returns the removed paths. Never raises."""
    from coordinator_core.group_em.session_registry import pid_alive
    from coordinator_core.ops.dispatch_emit.run_close import bare_run_id
    from coordinator_core.testing.baseline_export import remove

    root = Path(root)
    removed: list[str] = []
    keep = bare_run_id(keep_run) if keep_run else None
    warp = root / "scratch" / "warp"
    try:
        with os.scandir(warp) as it:
            run_ids = [e.name for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        run_ids = []
    for run_id in run_ids:
        try:
            if keep is not None and (run_id == keep_run or bare_run_id(run_id) == keep):
                continue
            targets = [warp / run_id / n for n in EXPORT_DIR_NAMES if os.path.lexists(warp / run_id / n)]
            if not targets or _run_referenced(root, run_id):
                continue
            for target in targets:
                if remove(target):
                    removed.append(str(target))
        except Exception:
            continue
    try:
        with os.scandir(root / "scratch") as it:
            bases = [e.name for e in it if _TEST_BASE_RE.match(e.name)]
    except OSError:
        bases = []
    for name in bases:
        try:
            if not pid_alive(int(_TEST_BASE_RE.match(name).group(1))) and remove(root / "scratch" / name):
                removed.append(str(root / "scratch" / name))
        except Exception:
            continue
    return removed
