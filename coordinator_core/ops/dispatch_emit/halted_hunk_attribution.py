"""Which dirty paths a halted run declares still carry hunks attributable to that run?

`reader(root, landing_run_id)` answers, per halted holder H and landing run L, the subset of a
holder's dirty declared paths that H's own work explains. The rules, first match decides:

1. p not in H's `write_baseline` (or H's manifest unreadable): hold -- unknown is not unchanged.
2. the tree's blob equals H's baseline for p: release -- H left nothing.
2b. the tree's blob equals a blob H's `kept.json` fingerprints for p: hold -- the hunks are H's.
3. L unknown, p not in L's `write_baseline`, or H's halt not provably before L's start: hold.
4. L's baseline equals H's for p: release -- the tree delta is L's own.
5. the index entry for p equals L's baseline: release -- bH to bL is committed history.
6. p is in L's own checkpoint-landed paths: release.
7. anything else: hold.

Invariants: zero spawns (file reads and one index parse); never raises -- any read error holds;
a path H's halt record withholds is the caller's to hold and is never decided here. Stdlib only at
module top (the land_run / terminal_commit cold-import pins); every reader is imported lazily.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional


def _norm(path: object) -> str:
    text = str(path or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _manifest(root: Path, run_id: str) -> dict:
    """`manifest.json` of `run_id`: the record dir first, then the RUN_DIR_ROOT copy."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, record_dir

    try:
        data = _read_json(record_dir(root, run_id) / "manifest.json")
    except ValueError:
        data = {}
    return data or _read_json(Path(root) / RUN_DIR_ROOT / run_id / "manifest.json")


def _baseline(root: Path, run_id: str) -> Optional[dict]:
    """What bytes `run_id` staged against: `{forward-slash path: blob sha | None}`, or None when
    the manifest is absent, unreadable or carries no `write_baseline` mapping."""
    raw = _manifest(root, run_id).get("write_baseline")
    if not isinstance(raw, dict):
        return None
    return {_norm(p): sha for p, sha in raw.items() if isinstance(p, str) and (sha is None or isinstance(sha, str))}


def _iso_epoch(value: object) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _halt_epoch(root: Path, run_id: str) -> Optional[float]:
    """When `run_id` stopped writing: `run-stage.json` `last_activity_at` while its stage is
    `halted`, else the `halted.json` mtime (a later rewrite only moves this later)."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_STAGE_FILE, record_dir
    from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE

    try:
        rdir = record_dir(root, run_id)
    except ValueError:
        return None
    stage = _read_json(rdir / RUN_STAGE_FILE)
    if stage.get("stage") == "halted":
        epoch = _iso_epoch(stage.get("last_activity_at"))
        if epoch is not None:
            return epoch
    try:
        return (rdir / HALTED_FILE).stat().st_mtime
    except OSError:
        return None


def _start_epoch(root: Path, run_id: str) -> Optional[float]:
    """When `run_id` started: `run-stage.json` `started_at`, else the run-id stamp."""
    from coordinator_core.ops.dispatch_emit import stopped_runs
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_STAGE_FILE, record_dir

    try:
        epoch = _iso_epoch(_read_json(record_dir(root, run_id) / RUN_STAGE_FILE).get("started_at"))
    except ValueError:
        epoch = None
    return epoch if epoch is not None else stopped_runs._stamp_epoch(run_id)


def reader(
    root: Path, landing_run_id: Optional[str], *, landing_plan: Optional[str] = None,
) -> Callable[[str, Iterable[str]], set]:
    """`attributable(halted_run_id, paths) -> set`: the forward-slash `paths` that rules 1-7 hold
    for the halted run. The caller passes only paths already known dirty and not withheld. The
    landing run's baseline, start epoch, index snapshot and checkpoint-landed paths are each read
    at most once per reader, and only when a rule reaches them. Never raises."""
    root = Path(root)
    landing_memo: dict = {}
    index_memo: list = []

    def landing_baseline() -> Optional[dict]:
        if "baseline" not in landing_memo:
            landing_memo["baseline"] = _baseline(root, landing_run_id) if landing_run_id else None
        return landing_memo["baseline"]

    def landing_start() -> Optional[float]:
        if "start" not in landing_memo:
            landing_memo["start"] = _start_epoch(root, landing_run_id) if landing_run_id else None
        return landing_memo["start"]

    def index_entries() -> Optional[dict]:
        if not index_memo:
            from coordinator_core.git import git_state

            try:
                index_memo.append({_norm(p): e.sha for p, e in git_state.read_index(root).items()})
            except (git_state.IndexParseError, OSError):
                index_memo.append(None)
        return index_memo[0]

    def checkpoint_landed(wanted: set) -> frozenset:
        if not landing_plan:
            return frozenset()
        from coordinator_core.ops.dispatch_emit import own_checkpoint_paths

        base = _manifest(root, landing_run_id).get("head_sha") if landing_run_id else None
        return own_checkpoint_paths.checkpoint_landed_paths(
            root, landing_plan, base if isinstance(base, str) else None, sorted(wanted),
        )

    def kept_blobs(halted_run_id: str, p: str) -> set:
        """Blob shas H's `kept.json` fingerprints for `p`; empty when absent or unreadable."""
        from coordinator_core.ops.dispatch_emit import kept_work
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        try:
            return set(kept_work.kept_fingerprints(record_dir(root, halted_run_id)).get(p, frozenset()))
        except ValueError:
            return set()

    def attributable(halted_run_id: str, paths: Iterable[str]) -> set:
        wanted = {_norm(p) for p in paths if p}
        wanted.discard("")
        try:
            from coordinator_core.ops.dispatch_emit import kept_work
            from coordinator_core.ops.dispatch_emit.kept_work import fingerprint

            b_h = _baseline(root, halted_run_id)
            if b_h is None:
                return wanted
            memo: dict = {}

            def halted_before_landing() -> bool:
                if "order" not in memo:
                    halt, start = _halt_epoch(root, halted_run_id), landing_start()
                    memo["order"] = halt is not None and start is not None and halt < start
                return memo["order"]

            def landed(p: str) -> bool:
                if "landed" not in memo:
                    memo["landed"] = checkpoint_landed(wanted)
                return p in memo["landed"]

            def holds(p: str) -> bool:
                if p not in b_h:
                    return True
                current = fingerprint(root, p)
                if current == b_h[p]:
                    return False
                if current is not None and current in kept_blobs(halted_run_id, p):
                    return True
                b_l = landing_baseline()
                if b_l is None or p not in b_l or not halted_before_landing():
                    return True
                if b_l[p] == b_h[p]:
                    return False
                entries = index_entries()
                if (
                    entries is not None
                    and entries.get(p) is not None
                    and kept_work.matches_committed(root, p, b_l[p], entries[p])
                ):
                    return False
                return not landed(p)

            held = set()
            for p in sorted(wanted):
                try:
                    if holds(p):
                        held.add(p)
                except Exception:  # noqa: BLE001 -- unreadable evidence holds
                    held.add(p)
            return held
        except Exception:  # noqa: BLE001 -- unreadable evidence holds
            return wanted

    return attributable
