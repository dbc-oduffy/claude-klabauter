"""Batching and partial credit for the reverify test leg.

The leg's budget is fixed (`test_leg_run.TEST_LEG_MINUTES_BUDGET`) and is never raised to fit a suite. A
target set that cannot finish in it is split into sequential legs (one pytest process per leg, never one
per test), every test that finished before the deadline is credited from `leg_reporter`'s event lines, and
the files that cannot fit are named with their seconds so their owner fixes them.

Durations are recorded per file in the engine's runtime dir (`durations_path`), keyed by repo root from each leg's own reports; with
none recorded, legs split by file count.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Sequence

from coordinator_core.testing.leg_reporter import RESULTS_ENV

#: Files of unknown duration one leg takes before it closes; a known-fast set never splits on count.
UNKNOWN_FILES_PER_LEG = 20
#: The reporter is loaded by file path in the pytest child: the repo under test need not import `coordinator_core`.
PLUGIN_FILE = str(Path(__file__).with_name("leg_reporter.py"))


class LegTimeout(subprocess.TimeoutExpired):
    """A deadline expiry carrying `credit`: `done` (every finished test event), `unrun` (targets with no
    result), `slow` ([{file, seconds}]: over-cap files, plus the file in flight at the deadline)."""

    credit: dict


def file_of(node: str) -> str:
    return re.split(r"::| > ", node.replace("\\", "/"), maxsplit=1)[0].strip()


def durations_path(cwd: str) -> Path | None:
    """The per-repo durations file in the engine's runtime dir (`breadcrumb.svc_dir`), never under `cwd`: the repo
    under test can be a consumer repo the engine must not write into. None: no cache dir, memory-only."""
    try:
        from coordinator_core.warm import breadcrumb

        key = hashlib.sha1(str(cwd).encode("utf-8", "replace")).hexdigest()[:16]
        return breadcrumb.svc_dir() / "test-leg-durations" / f"{key}.json"
    except Exception:  # noqa: BLE001
        return None


def load_durations(cwd: str) -> dict[str, float]:
    path = durations_path(cwd)
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}
    return {k: float(v) for k, v in raw.items() if isinstance(v, (int, float))} if isinstance(raw, dict) else {}


def _save_durations(cwd: str, durations: dict[str, float]) -> None:
    path = durations_path(cwd)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(durations, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def plan_legs(targets: Sequence[str], durations: dict[str, float], cap_s: float
              ) -> tuple[list[list[str]], list[dict]]:
    """(legs, slow). A bare file whose recorded duration alone exceeds `cap_s` is `slow` ({file, seconds}) and
    joins no leg. The rest pack first-fit in order: a leg closes when its recorded seconds would pass `cap_s`
    or it holds `UNKNOWN_FILES_PER_LEG` files of no record. Node ids of one file stay together."""
    by_file: dict[str, list[str]] = {}
    for t in targets:
        by_file.setdefault(file_of(t), []).append(t)
    legs: list[list[str]] = []
    slow: list[dict] = []
    cur: list[str] = []
    est, unknown = 0.0, 0
    for f, nodes in by_file.items():
        d = durations.get(f)
        if d is not None and d > cap_s and all(n.replace("\\", "/") == f for n in nodes):
            slow.append({"file": f, "seconds": round(d, 1)})
            continue
        if cur and ((d is not None and est + d > cap_s) or (d is None and unknown >= UNKNOWN_FILES_PER_LEG)):
            legs.append(cur)
            cur, est, unknown = [], 0.0, 0
        cur.extend(nodes)
        if d is None:
            unknown += 1
        else:
            est += d
    if cur:
        legs.append(cur)
    return legs, slow


def read_events(path: Path) -> list[dict]:
    """Complete event lines; a half-written last line (killed mid-write) is dropped."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _tally_events(events: Sequence[dict], now: float) -> dict:
    """done events, ids collected but unfinished, seconds per file (finished tests; the in-flight file's
    elapsed time is added to `spent` and named in `in_flight`)."""
    done = [e for e in events if e.get("k") == "done"]
    finished = {e["id"] for e in done}
    collected = [i for e in events if e.get("k") == "collected" for i in e.get("ids", [])]
    spent: dict[str, float] = {}
    for e in done:
        spent[file_of(e["id"])] = spent.get(file_of(e["id"]), 0.0) + float(e.get("dur") or 0.0)
    running = [e for e in events if e.get("k") == "start" and e["id"] not in finished]
    flying = file_of(running[-1]["id"]) if running else None
    if running and flying:
        spent[flying] = spent.get(flying, 0.0) + max(now - float(running[-1]["t"]), 0.0)
    return {"done": done, "unfinished": [i for i in collected if i not in finished], "collected": bool(collected),
            "spent": spent, "in_flight": flying}


def _record(cwd: str, durations: dict[str, float], spent: dict[str, float], in_flight: str | None) -> None:
    """Fold per-file seconds into the store; the in-flight file is a lower bound and only ever raises a record."""
    if not spent:
        return
    merged = dict(durations)
    for f, s in spent.items():
        merged[f] = max(merged.get(f, 0.0), s) if f == in_flight else s
        merged[f] = round(merged[f], 2)
    _save_durations(cwd, merged)


def run_legs(targets: Sequence[str], cwd: str, pytest_fn: Callable, remaining: Callable[[], float],
             cap_s: float, scratch: Path) -> tuple[int, list[str], dict]:
    """Run `targets` as sequential legs through `pytest_fn(leg_targets, cwd, seconds_left)`; (rc, lines, info).

    `info`: `slow` (over-cap files the plan excluded) and `legs` (count run). One leg returns pytest's own
    output untouched. A deadline expiry raises `LegTimeout` with `credit` for everything that finished."""
    durations = load_durations(cwd)
    legs, slow = plan_legs(targets, durations, cap_s)
    scratch.mkdir(parents=True, exist_ok=True)
    outs: list[tuple[int, list[str]]] = []
    all_done: list[dict] = []
    spent: dict[str, float] = {}
    prior = os.environ.get(RESULTS_ENV)
    try:
        for n, leg in enumerate(legs):
            path = scratch / f"leg-{n}.jsonl"
            path.unlink(missing_ok=True)
            os.environ[RESULTS_ENV] = str(path)
            try:
                rc, lines = pytest_fn(leg, cwd, remaining())
            except subprocess.TimeoutExpired as exc:
                t = _tally_events(read_events(path), time.time())
                all_done.extend(t["done"])
                spent.update(t["spent"])
                _record(cwd, durations, spent, t["in_flight"])
                unfinished_files = {file_of(i) for i in t["unfinished"]}
                unrun = [x for x in leg if not t["collected"] or file_of(x) in unfinished_files]
                unrun += [x for later in legs[n + 1:] for x in later]
                flying = [{"file": t["in_flight"], "seconds": round(spent[t["in_flight"]], 1)}] if t["in_flight"] else []
                err = LegTimeout(exc.cmd, exc.timeout)
                err.credit = {"done": all_done, "unrun": unrun, "slow": [*slow, *flying]}
                raise err from exc
            t = _tally_events(read_events(path), time.time())
            all_done.extend(t["done"])
            spent.update(t["spent"])
            outs.append((rc, lines))
    finally:
        if prior is None:
            os.environ.pop(RESULTS_ENV, None)
        else:
            os.environ[RESULTS_ENV] = prior
        shutil.rmtree(scratch, ignore_errors=True)
    _record(cwd, durations, spent, None)
    info = {"slow": slow, "legs": len(legs)}
    if not outs:
        return 5, [], info
    if len(outs) == 1:
        return outs[0][0], outs[0][1], info
    rcs = [r for r, _ in outs]
    rc = next((r for r in rcs if r not in (0, 5)), 0 if 0 in rcs else 5)
    passed = sum(1 for e in all_done if e["outcome"] == "passed")
    failed = sum(1 for e in all_done if e["outcome"] == "failed")
    return rc, [*(ln for _, ls in outs for ln in ls), f"{passed} passed, {failed} failed in 0s"], info


def credit_fields(credit: dict) -> dict:
    """Result fields for a leg that hit its deadline: counts of what finished, `failing` with detail,
    `unrun_targets`, `slow_targets`. `tests_run` is never 0 when a test finished."""
    done = credit["done"]
    failed = [e for e in done if e["outcome"] == "failed"]
    return {
        "tests_run": sum(1 for e in done if e["outcome"] in ("passed", "failed")),
        "tests_failed": len(failed),
        "failing": [e["id"] for e in failed],
        "failing_detail": [{"node_id": e["id"], "message": (e.get("msg") or "failed before the deadline").strip()}
                           for e in failed],
        "unrun_targets": list(dict.fromkeys(credit["unrun"])),
        "slow_targets": credit["slow"],
    }
