"""pytest plugin for the reverify test leg: one JSON line per event, appended as it happens.

`--junitxml` is written only at pytest's exit, so a leg killed at its deadline loses every result.
This reporter flushes each event as it occurs, so the parent credits every test that finished.
Inert unless `TEST_LEG_RESULTS` names a file.

Lines: `{"k":"collected","ids":[...]}`, `{"k":"start","id","t"}`, `{"k":"done","id","outcome","dur","msg"}`
(`dur` sums setup, call and teardown; `outcome` is passed, failed or skipped).
"""
from __future__ import annotations

import json
import os
import time

RESULTS_ENV = "TEST_LEG_RESULTS"

_out = None
_state: dict[str, dict] = {}


def _emit(rec: dict) -> None:
    if _out is not None:
        _out.write(json.dumps(rec) + "\n")
        _out.flush()


def pytest_configure(config) -> None:
    global _out
    path = os.environ.get(RESULTS_ENV)
    _out = open(path, "a", encoding="utf-8") if path else None
    _state.clear()


def pytest_unconfigure(config) -> None:
    global _out
    if _out is not None:
        _out.close()
        _out = None


def pytest_collection_modifyitems(items) -> None:
    _emit({"k": "collected", "ids": [i.nodeid for i in items]})


def pytest_runtest_logstart(nodeid, location) -> None:
    _emit({"k": "start", "id": nodeid, "t": time.time()})


def pytest_runtest_logreport(report) -> None:
    st = _state.setdefault(report.nodeid, {"dur": 0.0, "outcome": "passed", "msg": ""})
    st["dur"] += float(getattr(report, "duration", 0.0) or 0.0)
    if report.failed:
        st["outcome"] = "failed"
        crash = getattr(getattr(report, "longrepr", None), "reprcrash", None)
        st["msg"] = st["msg"] or str(getattr(crash, "message", "") or report.longreprtext[-300:])
    elif report.skipped and st["outcome"] == "passed":
        st["outcome"] = "skipped"
    if report.when == "teardown":
        done = _state.pop(report.nodeid)
        _emit({"k": "done", "id": report.nodeid, "outcome": done["outcome"], "dur": round(done["dur"], 4),
               "msg": done["msg"]})
