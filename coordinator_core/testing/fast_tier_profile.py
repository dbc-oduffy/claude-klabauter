"""Fast-tier overhead profile: where a pytest-xdist run spends wall time that no test owns.

Active only when COORDINATOR_FAST_TIER_PROFILE names an output path; conftest.py calls
`register(config)`, which returns None and registers nothing otherwise. Each worker writes
`<out>.<worker>.part` at session finish; the controller merges the parts into `<out>` and
removes them. `fast-tier-budget.py record --profile <out>` consumes the merged JSON.
"""

from __future__ import annotations

import glob
import json
import os
import time
from pathlib import Path
from typing import Mapping, Optional

import pytest

ENV_KEY = "COORDINATOR_FAST_TIER_PROFILE"
PLUGIN_NAME = "fast_tier_profile"


def merge(workers: list[dict], wall_s: float) -> dict:
    """Controller view of per-worker records. `tail_idle_s` = wall minus the busiest worker's busy seconds.

    `session_setup_s` is session start to first test, so it contains `collection_s`; the constant
    per-worker overhead is `session_setup_s + worker_tail_s`, not the sum of all three.
    """
    ran = [w for w in workers if w.get("tests", 0) > 0] or workers
    max_busy = max((w["busy_s"] for w in ran), default=0.0)
    return {
        "wall_s": round(wall_s, 3),
        "workers": len(ran),
        "collection_s": round(max((w["collection_s"] for w in ran), default=0.0), 3),
        "session_setup_s": round(max((w["session_setup_s"] for w in ran), default=0.0), 3),
        "worker_tail_s": round(max((w["tail_s"] for w in ran), default=0.0), 3),
        "max_busy_s": round(max_busy, 3),
        "sum_busy_s": round(sum(w["busy_s"] for w in ran), 3),
        "tail_idle_s": round(max(wall_s - max_busy, 0.0), 3),
        "per_worker": sorted(ran, key=lambda w: str(w.get("worker"))),
    }


class FastTierProfile:
    """Records one process's collection, setup, busy and tail seconds; merges on the controller."""

    def __init__(self, out: Path, worker: Optional[str]) -> None:
        self.out = out
        self.worker = worker
        self.t_start = time.perf_counter()
        self.collection_s = 0.0
        self.first_test: Optional[float] = None
        self.last_end: Optional[float] = None
        self.busy_s = 0.0
        self.tests = 0

    def _parts(self) -> list[str]:
        return glob.glob(glob.escape(str(self.out)) + ".*.part")

    @pytest.hookimpl(hookwrapper=True)
    def pytest_collection(self):
        t0 = time.perf_counter()
        yield
        self.collection_s += time.perf_counter() - t0

    def pytest_sessionstart(self, session) -> None:
        self.t_start = time.perf_counter()
        if self.worker is None:
            for part in self._parts():
                os.unlink(part)

    def pytest_runtest_logstart(self, nodeid, location) -> None:
        if self.first_test is None:
            self.first_test = time.perf_counter()

    def pytest_runtest_logreport(self, report) -> None:
        self.busy_s += report.duration
        if report.when == "teardown":
            self.tests += 1

    def pytest_runtest_logfinish(self, nodeid, location) -> None:
        self.last_end = time.perf_counter()

    def record(self) -> dict:
        now = time.perf_counter()
        return {
            "worker": self.worker or "controller",
            "tests": self.tests,
            "collection_s": round(self.collection_s, 3),
            "session_setup_s": round((self.first_test or now) - self.t_start, 3),
            "busy_s": round(self.busy_s, 3),
            "tail_s": round(now - (self.last_end or now), 3),
        }

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus) -> None:
        self.out.parent.mkdir(parents=True, exist_ok=True)
        if self.worker is not None:
            Path(f"{self.out}.{self.worker}.part").write_text(json.dumps(self.record()), encoding="utf-8")
            return
        records = [self.record()] if self.tests else []
        for part in self._parts():
            records.append(json.loads(Path(part).read_text(encoding="utf-8")))
            os.unlink(part)
        merged = merge(records, time.perf_counter() - self.t_start)
        self.out.write_text(json.dumps(merged, indent=1) + "\n", encoding="utf-8")


def register(config, environ: Optional[Mapping[str, str]] = None) -> Optional[FastTierProfile]:
    """Register the profile plugin when ENV_KEY names an output path; return None and do nothing otherwise."""
    out = (os.environ if environ is None else environ).get(ENV_KEY)
    if not out:
        return None
    workerinput = getattr(config, "workerinput", None)
    worker = workerinput["workerid"] if workerinput else None
    plugin = FastTierProfile(Path(out), worker)
    config.pluginmanager.register(plugin, PLUGIN_NAME)
    return plugin
